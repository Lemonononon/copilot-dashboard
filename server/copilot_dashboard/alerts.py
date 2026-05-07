"""Lightweight alert detection over parsed sessions.

Each alert is a small dict ready for JSON. Severity is one of:
    info  — informational, blue
    warn  — needs attention, amber
    error — failure / critical, red

The detector is purely functional: given a SessionParsed it returns alerts
about *that* session. Aggregation and routing happens in the store / API.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Iterable

from .parser import SessionParsed, Step


# Tunable thresholds (seconds).
LONG_TOOL_INFO_S = 60          # any tool taking >60s
LONG_TOOL_WARN_S = 300         # >5min
PENDING_TOOL_WARN_S = 120      # currently-pending tool stalled this long
ASKQ_LONG_S = 300              # askQuestions waiting > 5min
CONSECUTIVE_FAIL_THRESHOLD = 3 # N back-to-back tool failures


def detect(parsed: SessionParsed, now: float | None = None) -> list[dict]:
    """Return a list of alert dicts for one session."""
    if now is None:
        now = time.time()
    alerts: list[dict] = []
    sid = parsed.session_id

    def add(severity: str, kind: str, label: str, *, hint: str = "",
            ts: float = 0.0, step_index: int | None = None,
            tool: str | None = None) -> None:
        alerts.append({
            "id": f"{sid}:{kind}:{step_index if step_index is not None else int(ts)}",
            "session_id": sid,
            "severity": severity,
            "kind": kind,
            "label": label,
            "hint": hint,
            "ts": ts or parsed.last_event_at,
            "step_index": step_index,
            "tool": tool,
        })

    # 1) Consecutive tool failures.
    streak = 0
    streak_start_idx = None
    for i, s in enumerate(parsed.steps):
        if s.kind != "tool" or s.success is None:
            continue
        if s.success is False:
            if streak == 0:
                streak_start_idx = i
            streak += 1
            if streak == CONSECUTIVE_FAIL_THRESHOLD:
                add("warn", "consecutive_failures",
                    f"{streak}× consecutive tool failures",
                    hint=f"Starting at step {streak_start_idx}, tools: "
                         + ", ".join(
                             parsed.steps[j].tool_name or "?"
                             for j in range(streak_start_idx, i + 1)
                         )[:300],
                    ts=parsed.steps[streak_start_idx].ts if streak_start_idx is not None else s.ts,
                    step_index=streak_start_idx,
                    tool=s.tool_name)
        else:
            streak = 0
            streak_start_idx = None

    # 2) Slow individual completed tool calls.
    for i, s in enumerate(parsed.steps):
        if s.kind != "tool" or s.duration_ms is None:
            continue
        # askQuestions waiting on user is normal — skip unless extreme.
        if s.tool_name == "vscode_askQuestions":
            if s.duration_ms >= ASKQ_LONG_S * 1000:
                add("info", "long_user_wait",
                    f"User idle on prompt for {s.duration_ms // 1000}s",
                    hint=_first_question(s),
                    ts=s.ts, step_index=i, tool=s.tool_name)
            continue
        if s.duration_ms >= LONG_TOOL_WARN_S * 1000:
            add("warn", "slow_tool",
                f"{s.tool_name} took {s.duration_ms // 1000}s",
                hint=_arg_hint(s),
                ts=s.ts, step_index=i, tool=s.tool_name)
        elif s.duration_ms >= LONG_TOOL_INFO_S * 1000:
            add("info", "slow_tool",
                f"{s.tool_name} took {s.duration_ms // 1000}s",
                hint=_arg_hint(s),
                ts=s.ts, step_index=i, tool=s.tool_name)

    # 3) Currently-pending tool stuck.
    if parsed.in_progress and parsed.activity_state == "running_tool":
        age = max(0.0, now - parsed.activity_since)
        if age >= PENDING_TOOL_WARN_S:
            add("warn", "stuck_tool",
                f"Tool running for {int(age)}s and not yet returned",
                hint=parsed.activity_label,
                ts=parsed.activity_since,
                tool=parsed.activity_label.replace("Running ", ""))

    # 4) Repeated identical run_in_terminal failures (possible loop).
    cmd_fails: Counter = Counter()
    for s in parsed.steps:
        if (s.kind == "tool" and s.tool_name == "run_in_terminal"
                and s.success is False and isinstance(s.arguments, dict)):
            cmd = (s.arguments.get("command") or "").strip()[:200]
            if cmd:
                cmd_fails[cmd] += 1
    for cmd, n in cmd_fails.most_common(3):
        if n >= 3:
            add("warn", "repeat_terminal_fail",
                f"Same terminal command failed {n}×",
                hint=cmd,
                ts=parsed.last_event_at, tool="run_in_terminal")

    # 5) Awaiting input for a while (info level only).
    if parsed.in_progress and parsed.activity_state == "awaiting_input":
        age = max(0.0, now - parsed.activity_since)
        if age >= ASKQ_LONG_S:
            add("info", "awaiting_input_long",
                f"Awaiting your input for {int(age)}s",
                hint=parsed.activity_detail[:300],
                ts=parsed.activity_since, tool="vscode_askQuestions")

    return alerts


def _first_question(s: Step) -> str:
    a = s.arguments
    if not isinstance(a, dict):
        return ""
    qs = a.get("questions")
    if qs and isinstance(qs[0], dict):
        return (qs[0].get("question") or qs[0].get("message") or "")[:300]
    return ""


def _arg_hint(s: Step) -> str:
    a = s.arguments
    if not isinstance(a, dict):
        return ""
    if s.tool_name == "run_in_terminal":
        return (a.get("command") or "")[:300]
    if s.tool_name == "read_file":
        return a.get("filePath") or ""
    if s.tool_name in ("grep_search", "file_search", "semantic_search"):
        return (a.get("query") or a.get("pattern") or "")[:200]
    if s.tool_name in ("create_file", "replace_string_in_file", "multi_replace_string_in_file"):
        return (a.get("filePath") or a.get("explanation") or "")[:200]
    return ""


SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}


def sort_key(alert: dict):
    return (SEVERITY_RANK.get(alert["severity"], 9), -float(alert.get("ts") or 0))
