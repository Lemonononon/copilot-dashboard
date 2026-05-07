"""Parse a Copilot transcript jsonl into a structured timeline.

Event shapes (observed):
    session.start          {sessionId, version, producer, copilotVersion, vscodeVersion, startTime}
    user.message           {content, attachments?}
    assistant.turn_start   {turnId}
    assistant.message      {messageId, content, toolRequests:[{toolCallId,name,arguments}], reasoningText?}
    tool.execution_start   {toolCallId, toolName, arguments(obj)}
    tool.execution_complete{toolCallId, success}
    assistant.turn_end     {turnId}

Every record has: type, data, id, timestamp, parentId.

The parser produces a flat ordered list of "steps" and aggregates per-tool stats.
A step is one of:
    user        — user prompt
    reasoning   — assistant's reasoning (chain-of-thought summary)
    say         — assistant's user-visible message text
    tool        — a tool call (paired start/complete with duration)

Subagent calls (toolName == "runSubagent") are represented as `tool` steps but
flagged so the UI can render them specially. The subagent's own internal trace
is not in this transcript (the agent returns a single summary message), but we
expose its `prompt` argument and final result presence.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .discovery import iter_jsonl


def _parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        # ISO 8601 with Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# Sentence-level decision triggers extracted from reasoning text. The goal is
# not perfect NLP; it's surfacing the moments where the agent visibly chose,
# rejected, switched approach, or noted a constraint — the things a reviewer
# would want to skim. Patterns are tested against trimmed sentences.
_DECISION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("plan",     re.compile(r"^(?:let me|i'?ll|i will|i need to|i should|next,?\s+i)\b", re.I)),
    ("switch",   re.compile(r"\b(?:instead of|rather than|switch(?:ing)? to|fall ?back to|pivot to)\b", re.I)),
    ("reject",   re.compile(r"\b(?:that won'?t work|this won'?t work|that'?s wrong|that doesn'?t|won'?t do|abandon|give up on|skip(?:ping)?)\b", re.I)),
    ("issue",    re.compile(r"\b(?:the (?:issue|problem|bug|root cause|culprit) is|turns out|actually,?|wait,?|hmm,?)\b", re.I)),
    ("conclude", re.compile(r"\b(?:therefore,?|so the answer|in conclusion|so we|so i'?ll|to summari[sz]e)\b", re.I)),
    ("verify",   re.compile(r"\b(?:let me verify|let me check|let me confirm|to confirm|double[- ]check)\b", re.I)),
]
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'\(\u4e00-\u9fff])")


def _extract_decisions(text: str, max_items: int = 6, max_len: int = 240) -> list[dict]:
    """Extract decision-point fragments from a reasoning blob.

    Returns a list of {kind, text} dicts (kind is one of plan/switch/reject/
    issue/conclude/verify). Heuristic, not exhaustive — meant to surface
    skim-worthy moments without spamming the UI.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    # Look at the first N sentences to keep this O(short).
    sentences = _SENTENCE_SPLIT.split(text.strip())[:40]
    for raw in sentences:
        s = raw.strip()
        if len(s) < 6 or len(s) > 600:
            continue
        for kind, pat in _DECISION_PATTERNS:
            if pat.search(s):
                clean = s if len(s) <= max_len else s[: max_len - 1] + "\u2026"
                key = (kind, clean[:80])
                if key in seen:
                    break
                seen.add(key)
                out.append({"kind": kind, "text": clean})
                break
        if len(out) >= max_items:
            break
    return out


@dataclass
class Step:
    kind: str                 # 'user' | 'reasoning' | 'say' | 'tool'
    ts: float                 # epoch seconds
    text: str = ""            # main content
    # tool-only fields
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: Any = None
    success: bool | None = None
    duration_ms: int | None = None
    is_subagent: bool = False
    # turn linkage (for grouping in UI)
    turn_id: str | None = None
    # NLP-tagged decision-point fragments extracted from reasoning text.
    # Empty for non-reasoning steps.
    decisions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # trim very large argument blobs in API output? Keep here, trim at API layer.
        return d


@dataclass
class SubagentCall:
    """One runSubagent / *_subagent invocation, surfaced for the tree view."""
    tool_name: str
    tool_call_id: str | None
    started_at: float
    duration_ms: int | None
    success: bool | None
    parent_turn_id: str | None
    step_index: int                   # index into SessionParsed.steps
    description: str = ""             # short label
    prompt: str = ""                  # detailed prompt
    agent_name: str | None = None     # e.g. "Explore" if specified
    model: str | None = None          # if specified
    argument_hint: str | None = None  # if specified

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class TodoSnapshot:
    """Most-recent manage_todo_list snapshot for a session — used as a progress proxy."""
    ts: float
    items: list[dict]   # [{id, title, status}]
    counts: dict        # {total, completed, in_progress, not_started}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionParsed:
    session_id: str
    started_at: float = 0.0
    last_event_at: float = 0.0
    copilot_version: str | None = None
    vscode_version: str | None = None
    steps: list[Step] = field(default_factory=list)
    tool_stats: dict[str, dict] = field(default_factory=dict)
    turns: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    subagent_calls: int = 0
    in_progress: bool = False
    # live activity (filled by infer_activity)
    activity_state: str = "idle"   # idle|thinking|running_tool|awaiting_input|subagent
    activity_label: str = ""        # human-readable, e.g. "running run_in_terminal"
    activity_since: float = 0.0     # epoch seconds when this state began
    activity_detail: str = ""       # extra (e.g. tool args preview, prompt the agent is waiting on)
    # subagents (filled while parsing)
    subagents: list[SubagentCall] = field(default_factory=list)
    # latest todo list snapshot (progress proxy)
    todo: TodoSnapshot | None = None

    def to_summary(self) -> dict:
        last_user = next((s.text for s in reversed(self.steps) if s.kind == "user"), "")
        first_user = next((s.text for s in self.steps if s.kind == "user"), "")
        # Fallbacks when transcript has no user.message (e.g. the very first
        # chat-input prompt is not always logged as such): use the first
        # reasoning snippet, then the first assistant 'say'.
        if not first_user:
            first_user = next((s.text for s in self.steps if s.kind == "reasoning"), "")
        if not first_user:
            first_user = next((s.text for s in self.steps if s.kind == "say"), "")
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "copilot_version": self.copilot_version,
            "vscode_version": self.vscode_version,
            "turns": self.turns,
            "user_messages": self.user_messages,
            "assistant_messages": self.assistant_messages,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "subagent_calls": self.subagent_calls,
            "in_progress": self.in_progress,
            "first_user_message": first_user[:240],
            "last_user_message": last_user[:240],
            "activity": {
                "state": self.activity_state,
                "label": self.activity_label,
                "since": self.activity_since,
                "detail": self.activity_detail[:400],
            },
            "subagents_count": len(self.subagents),
            "todo": self.todo.to_dict() if self.todo else None,
        }


def parse_transcript(path: Path, in_progress_threshold_s: float = 120.0) -> SessionParsed:
    sp = SessionParsed(session_id=path.stem)
    pending_tools: dict[str, Step] = {}
    current_turn: str | None = None
    tool_durations: dict[str, list[int]] = defaultdict(list)
    tool_count: Counter = Counter()
    tool_fail: Counter = Counter()

    for rec in iter_jsonl(path):
        et = rec.get("type")
        data = rec.get("data") or {}
        ts = _parse_ts(rec.get("timestamp"))
        if ts > sp.last_event_at:
            sp.last_event_at = ts

        if et == "session.start":
            sp.started_at = _parse_ts(data.get("startTime")) or ts
            sp.copilot_version = data.get("copilotVersion")
            sp.vscode_version = data.get("vscodeVersion")

        elif et == "user.message":
            sp.user_messages += 1
            content = data.get("content") or ""
            if isinstance(content, list):
                # array of segments — flatten text-ish parts
                content = "\n".join(
                    seg.get("text", "") if isinstance(seg, dict) else str(seg)
                    for seg in content
                )
            sp.steps.append(Step(kind="user", ts=ts, text=str(content), turn_id=current_turn))

        elif et == "assistant.turn_start":
            current_turn = str(data.get("turnId"))
            sp.turns += 1

        elif et == "assistant.turn_end":
            current_turn = None

        elif et == "assistant.message":
            sp.assistant_messages += 1
            reasoning = data.get("reasoningText") or ""
            content = data.get("content") or ""
            if reasoning:
                sp.steps.append(Step(
                    kind="reasoning",
                    ts=ts,
                    text=reasoning,
                    turn_id=current_turn,
                    decisions=_extract_decisions(reasoning),
                ))
            if content:
                sp.steps.append(Step(kind="say", ts=ts, text=str(content), turn_id=current_turn))
            # tool requests are matched by tool.execution_start, but we also track
            # any requested-but-not-started ones (rare).

        elif et == "tool.execution_start":
            tcid = data.get("toolCallId")
            tname = data.get("toolName") or "?"
            args = data.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            is_sub = tname == "runSubagent" or tname.endswith("_subagent")
            step = Step(
                kind="tool",
                ts=ts,
                text=tname,
                tool_name=tname,
                tool_call_id=tcid,
                arguments=args,
                is_subagent=is_sub,
                turn_id=current_turn,
            )
            sp.steps.append(step)
            sp.tool_calls += 1
            tool_count[tname] += 1
            if step.is_subagent:
                sp.subagent_calls += 1
                a = args if isinstance(args, dict) else {}
                sp.subagents.append(SubagentCall(
                    tool_name=tname,
                    tool_call_id=tcid,
                    started_at=ts,
                    duration_ms=None,
                    success=None,
                    parent_turn_id=current_turn,
                    step_index=len(sp.steps) - 1,
                    description=str(a.get("description") or "")[:200],
                    prompt=str(a.get("prompt") or a.get("query") or "")[:4000],
                    agent_name=a.get("agentName"),
                    model=a.get("model"),
                    argument_hint=a.get("argumentHint"),
                ))
            # capture latest todo snapshot
            if tname == "manage_todo_list" and isinstance(args, dict):
                items_raw = args.get("todoList") or []
                items = []
                counts = {"total": 0, "completed": 0, "in_progress": 0, "not_started": 0}
                for it in items_raw:
                    if not isinstance(it, dict):
                        continue
                    status = str(it.get("status") or "not-started").replace("-", "_")
                    items.append({
                        "id": it.get("id"),
                        "title": str(it.get("title") or "")[:200],
                        "status": status,
                    })
                    counts["total"] += 1
                    if status == "completed":
                        counts["completed"] += 1
                    elif status == "in_progress":
                        counts["in_progress"] += 1
                    else:
                        counts["not_started"] += 1
                sp.todo = TodoSnapshot(ts=ts, items=items, counts=counts)
            if tcid:
                pending_tools[tcid] = step

        elif et == "tool.execution_complete":
            tcid = data.get("toolCallId")
            success = bool(data.get("success", True))
            step = pending_tools.pop(tcid, None)
            if step is not None:
                step.success = success
                step.duration_ms = max(0, int((ts - step.ts) * 1000)) if ts and step.ts else None
                if step.duration_ms is not None and step.tool_name:
                    tool_durations[step.tool_name].append(step.duration_ms)
                if not success and step.tool_name:
                    tool_fail[step.tool_name] += 1
                    sp.tool_failures += 1
                # finalize subagent record (search by tool_call_id)
                if step.is_subagent:
                    for sa in sp.subagents:
                        if sa.tool_call_id == tcid:
                            sa.duration_ms = step.duration_ms
                            sa.success = success
                            break

    # Finalize tool_stats
    stats: dict[str, dict] = {}
    for name, count in tool_count.items():
        durs = tool_durations.get(name, [])
        stats[name] = {
            "count": count,
            "failures": tool_fail.get(name, 0),
            "avg_ms": int(sum(durs) / len(durs)) if durs else None,
            "p95_ms": int(sorted(durs)[int(len(durs) * 0.95) - 1]) if len(durs) >= 5 else None,
            "total_ms": sum(durs) if durs else 0,
        }
    sp.tool_stats = stats

    # In-progress heuristic: file mtime is recent AND we have unmatched tools or
    # the last event isn't an assistant.turn_end. We approximate with mtime here.
    import time
    now = time.time()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = sp.last_event_at
    sp.in_progress = (
        len(pending_tools) > 0
        or (now - mtime) < in_progress_threshold_s
    )

    # Infer current activity by walking the tail of the transcript.
    sp.activity_state = "idle"
    sp.activity_label = ""
    sp.activity_since = sp.last_event_at
    sp.activity_detail = ""
    if sp.in_progress:
        if pending_tools:
            # Prefer the most recently started pending tool
            tool_step = max(pending_tools.values(), key=lambda s: s.ts)
            tname = tool_step.tool_name or "?"
            sp.activity_since = tool_step.ts or sp.last_event_at
            # If the "pending" tool is ancient (>10min) treat as orphan -> idle.
            stale = (now - sp.activity_since) > 600
            if stale:
                sp.activity_state = "idle"
                sp.activity_label = "Stalled / abandoned tool"
                sp.activity_detail = f"{tname} never returned"
                sp.in_progress = False
            elif tname == "vscode_askQuestions":
                sp.activity_state = "awaiting_input"
                args = tool_step.arguments
                q = ""
                if isinstance(args, dict):
                    qs = args.get("questions") or []
                    if qs and isinstance(qs[0], dict):
                        q = qs[0].get("question") or qs[0].get("message") or ""
                sp.activity_label = "Awaiting your input"
                sp.activity_detail = q
            elif tool_step.is_subagent:
                sp.activity_state = "subagent"
                args = tool_step.arguments
                desc = ""
                if isinstance(args, dict):
                    desc = (args.get("description")
                            or (args.get("prompt") or "")[:120])
                sp.activity_label = f"Subagent: {desc or 'running'}"
                sp.activity_detail = (args.get("prompt") if isinstance(args, dict) else "") or ""
            else:
                sp.activity_state = "running_tool"
                sp.activity_label = f"Running {tname}"
                # tool-specific detail preview
                args = tool_step.arguments
                if isinstance(args, dict):
                    if tname == "run_in_terminal":
                        sp.activity_detail = (args.get("command") or "")[:300]
                    elif tname == "read_file":
                        sp.activity_detail = (args.get("filePath") or "")
                    elif tname in ("grep_search", "file_search", "semantic_search"):
                        sp.activity_detail = (args.get("query") or args.get("pattern") or "")[:200]
                    elif tname in ("create_file", "replace_string_in_file", "multi_replace_string_in_file"):
                        sp.activity_detail = (args.get("filePath") or args.get("explanation") or "")[:200]
        else:
            # No pending tool: agent is most likely thinking (between tool calls)
            # OR the conversation is paused awaiting next user input.
            # Distinguish by: is there a recent assistant.turn_end? Then idle.
            # We tracked the last event type via sp.last_event_at + step list:
            last_real = sp.steps[-1] if sp.steps else None
            if last_real is None:
                sp.activity_state = "thinking"
                sp.activity_label = "Starting up"
                sp.activity_since = sp.last_event_at
            else:
                # if last step was 'say' and file is fresh -> agent finished a turn,
                # waiting for user. Mark as idle (in_progress flag stays true via mtime
                # threshold but state is idle so UI can de-emphasize).
                if (now - mtime) < 8:
                    sp.activity_state = "thinking"
                    sp.activity_label = "Thinking…"
                    sp.activity_since = last_real.ts
                else:
                    sp.activity_state = "idle"
                    sp.activity_label = "Waiting for next prompt"
                    sp.activity_since = last_real.ts
    return sp
