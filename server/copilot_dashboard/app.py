"""FastAPI app — REST + WebSocket."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import hidden as hidden_mod
from .store import Store

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


class HiddenBody(BaseModel):
    # kind: "session" | "workspace" | "archive" | "pin" | "alert"
    kind: str
    id: str
    hidden: bool


class HiddenBulkBody(BaseModel):
    kind: str
    ids: list[str]
    hidden: bool

# Trim heavy fields when listing/streaming.
_MAX_TEXT = 8000
_MAX_ARG_TEXT = 4000


def _trim_text(s: str, n: int = _MAX_TEXT) -> str:
    if not isinstance(s, str):
        s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[truncated {len(s) - n} chars]"


def _trim_args(args: Any) -> Any:
    import json
    try:
        rendered = json.dumps(args, ensure_ascii=False)
    except Exception:
        rendered = str(args)
    if len(rendered) <= _MAX_ARG_TEXT:
        return args
    return {"__truncated__": True, "preview": rendered[:_MAX_ARG_TEXT] + " …"}


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.attach_loop(asyncio.get_running_loop())
        store.refresh_index()
        store.start_watcher()
        store.start_ticker()
        try:
            yield
        finally:
            store.stop_ticker()
            store.stop_watcher()

    app = FastAPI(title="Copilot Dashboard", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "sessions": len(store.sessions()), "workspaces": len(store.workspaces())}

    @app.get("/api/activity")
    def activity() -> dict:
        import time
        return {"now": time.time(), "items": store.collect_activity()}

    @app.get("/api/alerts")
    def alerts(limit: int = 200) -> dict:
        import time
        return {"now": time.time(), "items": store.collect_alerts(limit=limit)}

    @app.get("/api/workspaces")
    def workspaces() -> list[dict]:
        return [
            {
                "hash": w.hash,
                "label": w.label,
                "short": w.short,
                "folder": w.folder,
                "workspace_file": w.workspace_file,
            }
            for w in store.workspaces()
        ]

    @app.get("/api/sessions")
    def sessions(
        workspace: str | None = None,
        limit: int = 200,
        include_hidden: bool = False,
        scope: str = "active",  # "active" | "archived" | "all"
    ) -> list[dict]:
        sets = hidden_mod.get_sets()
        hidden_sids = sets["sessions"]
        hidden_wss = sets["workspaces"]
        archived = sets["archived_sessions"]
        pinned = sets["pinned_sessions"]
        out: list[dict] = []
        for ref in store.sessions():
            if workspace and ref.workspace.hash != workspace:
                continue
            is_hidden = (
                ref.session_id in hidden_sids
                or ref.workspace.hash in hidden_wss
            )
            is_archived = ref.session_id in archived
            if scope == "archived":
                if not is_archived:
                    continue
            elif scope == "active":
                if is_archived:
                    continue
                if is_hidden and not include_hidden:
                    continue
            # scope == "all" passes everything
            parsed = store.get_parsed(ref.session_id)
            if parsed is None:
                continue
            summary = parsed.to_summary()
            summary["workspace"] = {
                "hash": ref.workspace.hash,
                "label": ref.workspace.label,
                "short": ref.workspace.short,
            }
            summary["mtime"] = ref.mtime
            summary["size"] = ref.size
            summary["hidden"] = is_hidden
            summary["hidden_via_workspace"] = (ref.workspace.hash in hidden_wss)
            summary["archived"] = is_archived
            summary["pinned"] = ref.session_id in pinned
            out.append(summary)
            if len(out) >= limit:
                break
        # pinned sessions float to top (within scope)
        out.sort(key=lambda s: (0 if s["pinned"] else 1, -s.get("last_event_at", 0)))
        return out

    # HiddenBody / HiddenBulkBody are defined at module scope (FastAPI requires this).

    @app.get("/api/hidden")
    def hidden_list() -> dict:
        d = hidden_mod.load()
        return {
            "sessions": d.get("sessions", []),
            "workspaces": d.get("workspaces", []),
            "archived_sessions": d.get("archived_sessions", []),
            "pinned_sessions": d.get("pinned_sessions", []),
            "dismissed_alerts": d.get("dismissed_alerts", []),
            "counts": {
                "sessions": len(d.get("sessions", [])),
                "workspaces": len(d.get("workspaces", [])),
                "archived_sessions": len(d.get("archived_sessions", [])),
                "pinned_sessions": len(d.get("pinned_sessions", [])),
                "dismissed_alerts": len(d.get("dismissed_alerts", [])),
            },
        }

    def _broadcast_hidden(data: dict) -> None:
        store.broadcast({
            "type": "hidden.updated",
            "sessions": data["sessions"],
            "workspaces": data["workspaces"],
            "archived_sessions": data["archived_sessions"],
            "pinned_sessions": data["pinned_sessions"],
            "dismissed_alerts": data["dismissed_alerts"],
        })

    @app.post("/api/hidden")
    def hidden_set(body: HiddenBody) -> dict:
        try:
            data = hidden_mod.set_one(body.kind, body.id, body.hidden)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _broadcast_hidden(data)
        return {"ok": True, **data}

    @app.post("/api/hidden/bulk")
    def hidden_bulk(body: HiddenBulkBody) -> dict:
        try:
            data = hidden_mod.set_many(body.kind, body.ids, body.hidden)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _broadcast_hidden(data)
        return {"ok": True, **data}

    @app.get("/api/session/{sid}")
    def session_detail(sid: str) -> dict:
        ref = store.get_session_ref(sid)
        if ref is None:
            raise HTTPException(404, "session not found")
        parsed = store.get_parsed(sid)
        if parsed is None:
            raise HTTPException(404, "session not parseable")
        steps_out = []
        for s in parsed.steps:
            d = s.to_dict()
            d["text"] = _trim_text(d.get("text") or "")
            if d.get("arguments") is not None:
                d["arguments"] = _trim_args(d["arguments"])
            steps_out.append(d)
        return {
            **parsed.to_summary(),
            "workspace": {
                "hash": ref.workspace.hash,
                "label": ref.workspace.label,
                "short": ref.workspace.short,
            },
            "tool_stats": parsed.tool_stats,
            "steps": steps_out,
            "subagents": [sa.to_dict() for sa in parsed.subagents],
        }

    @app.get("/api/session/{sid}/rehydrate")
    def session_rehydrate(sid: str) -> dict:
        """Markdown summary of a session, designed to be pasted into a *new* Copilot
        chat to bring a fresh agent up to speed.

        Includes: the user's original ask, the latest todo state, the most recent
        decisions parsed out of reasoning, and any unresolved tool failures.
        """
        parsed = store.get_parsed(sid)
        if parsed is None:
            raise HTTPException(404, "session not parseable")
        ref = store.get_session_ref(sid)
        first_user = next((s.text for s in parsed.steps if s.kind == "user"), "")
        last_user = next((s.text for s in reversed(parsed.steps) if s.kind == "user"), "")
        # collect last 5 distinct decisions
        decisions: list[dict] = []
        for s in reversed(parsed.steps):
            for d in (s.decisions or []):
                if not any(x["text"] == d["text"] for x in decisions):
                    decisions.append(d)
                if len(decisions) >= 5:
                    break
            if len(decisions) >= 5:
                break
        decisions.reverse()
        # collect recent failed tools
        recent_failures = []
        for s in reversed(parsed.steps):
            if s.kind == "tool" and s.success is False:
                recent_failures.append({
                    "tool": s.tool_name,
                    "args_preview": (str(s.arguments)[:200] if s.arguments else ""),
                })
                if len(recent_failures) >= 3:
                    break
        recent_failures.reverse()
        # build markdown
        lines = ["# Context recap (auto-generated by copilot-dashboard)", ""]
        if ref:
            lines.append(f"**Workspace:** `{ref.workspace.label}`")
        lines.append(f"**Original task:**")
        lines.append("")
        lines.append("> " + (first_user[:600] or "(no user message recorded)").replace("\n", "\n> "))
        lines.append("")
        if last_user and last_user != first_user:
            lines.append(f"**Most recent follow-up:**")
            lines.append("")
            lines.append("> " + last_user[:400].replace("\n", "\n> "))
            lines.append("")
        if parsed.todo and parsed.todo.items:
            lines.append("**Current todo state:**")
            lines.append("")
            for it in parsed.todo.items:
                mark = {"completed": "[x]", "in_progress": "[~]"}.get(it.get("status"), "[ ]")
                lines.append(f"- {mark} {it.get('title','')}")
            lines.append("")
        if decisions:
            lines.append("**Recent decisions made by the previous session:**")
            lines.append("")
            for d in decisions:
                lines.append(f"- *{d['kind']}* — {d['text']}")
            lines.append("")
        if recent_failures:
            lines.append("**Unresolved tool failures (most recent):**")
            lines.append("")
            for f in recent_failures:
                lines.append(f"- `{f['tool']}` — {f['args_preview'][:160]}")
            lines.append("")
        lines.append("**Stats:** "
                     f"{parsed.tool_calls} tool calls, "
                     f"{parsed.tool_failures} failures, "
                     f"{parsed.subagent_calls} subagents, "
                     f"{parsed.turns} turns.")
        lines.append("")
        lines.append("---")
        lines.append("Please continue from where this left off. If anything is unclear, ask before acting.")
        text = "\n".join(lines)
        return {
            "session_id": sid,
            "markdown": text,
            "char_count": len(text),
        }

    @app.get("/api/search")
    def search_sessions(q: str, limit: int = 50) -> dict:
        """Cross-session full-text search over user prompts, reasoning, and tool args.

        Returns matching sessions with up to 3 snippets each.
        """
        q = (q or "").strip()
        if len(q) < 2:
            return {"query": q, "results": [], "total": 0}
        ql = q.lower()
        results: list[dict] = []
        scanned = 0
        for ref in store.sessions():
            scanned += 1
            parsed = store.get_parsed(ref.session_id)
            if parsed is None:
                continue
            hits: list[dict] = []
            for i, s in enumerate(parsed.steps):
                fields = []
                if s.kind in ("user", "reasoning", "say") and s.text:
                    fields.append(("text", s.text))
                if s.kind == "tool" and s.arguments:
                    try:
                        fields.append(("args", json.dumps(s.arguments)[:1500]))
                    except Exception:
                        pass
                for tag, content in fields:
                    lo = content.lower().find(ql)
                    if lo < 0:
                        continue
                    start = max(0, lo - 60)
                    end   = min(len(content), lo + len(ql) + 100)
                    snippet = content[start:end].replace("\n", " ")
                    if start > 0: snippet = "…" + snippet
                    if end < len(content): snippet = snippet + "…"
                    hits.append({
                        "step_index": i,
                        "kind": s.kind,
                        "tag": tag,
                        "snippet": snippet,
                        "tool_name": s.tool_name,
                    })
                    if len(hits) >= 3:
                        break
                if len(hits) >= 3:
                    break
            if hits:
                summary = parsed.to_summary()
                results.append({
                    "session_id": ref.session_id,
                    "workspace": {
                        "hash": ref.workspace.hash,
                        "short": ref.workspace.short,
                        "label": ref.workspace.label,
                    },
                    "first_user_message": summary["first_user_message"],
                    "last_event_at": summary["last_event_at"],
                    "match_count": len(hits),
                    "hits": hits,
                })
                if len(results) >= limit:
                    break
        results.sort(key=lambda r: -r["last_event_at"])
        return {"query": q, "results": results, "total": len(results), "scanned": scanned}

    @app.get("/api/stats")
    def stats(workspace: str | None = None) -> dict:
        """Aggregate stats across (filtered) sessions."""
        tool_count: Counter = Counter()
        tool_fail: Counter = Counter()
        tool_dur: dict[str, list[int]] = {}
        sessions_n = 0
        in_progress_n = 0
        total_tool_calls = 0
        total_failures = 0
        subagent_calls = 0
        for ref in store.sessions():
            if workspace and ref.workspace.hash != workspace:
                continue
            parsed = store.get_parsed(ref.session_id)
            if parsed is None:
                continue
            sessions_n += 1
            if parsed.in_progress:
                in_progress_n += 1
            total_tool_calls += parsed.tool_calls
            total_failures += parsed.tool_failures
            subagent_calls += parsed.subagent_calls
            for name, st in parsed.tool_stats.items():
                tool_count[name] += st["count"]
                tool_fail[name] += st["failures"]
                if st.get("avg_ms") is not None:
                    tool_dur.setdefault(name, []).append(st["avg_ms"] * st["count"])
        top_tools = [
            {
                "name": name,
                "count": cnt,
                "failures": tool_fail.get(name, 0),
                "avg_ms": (
                    int(sum(tool_dur.get(name, [])) / max(1, cnt))
                    if tool_dur.get(name) else None
                ),
            }
            for name, cnt in tool_count.most_common(50)
        ]
        return {
            "sessions": sessions_n,
            "in_progress": in_progress_n,
            "total_tool_calls": total_tool_calls,
            "total_failures": total_failures,
            "subagent_calls": subagent_calls,
            "top_tools": top_tools,
        }

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        q = store.subscribe()
        try:
            await websocket.send_json({"type": "hello"})
            while True:
                event = await q.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            store.unsubscribe(q)

    # static frontend
    if WEB_DIR.is_dir():
        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app
