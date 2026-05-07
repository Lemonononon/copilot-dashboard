"""In-memory cache + filesystem watcher for the dashboard.

Responsibilities:
* index sessions (path -> SessionRef)
* parse transcripts on demand and cache results keyed by (path, mtime)
* watch transcripts dirs for changes and broadcast events to subscribers
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Awaitable, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .discovery import SessionRef, WorkspaceInfo, list_sessions, list_workspaces
from .parser import SessionParsed, parse_transcript
from . import alerts as alerts_mod
from . import hidden as hidden_mod


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workspaces: dict[str, WorkspaceInfo] = {}
        self._sessions: dict[str, SessionRef] = {}
        self._parse_cache: dict[str, tuple[float, SessionParsed]] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Observer | None = None
        self._tick_task: asyncio.Task | None = None

    # ---------------- sync data API ----------------
    def refresh_index(self) -> None:
        wss = list_workspaces()
        self._workspaces = {w.hash: w for w in wss}
        self._sessions = {s.session_id: s for s in list_sessions(wss)}

    def workspaces(self) -> list[WorkspaceInfo]:
        with self._lock:
            return list(self._workspaces.values())

    def sessions(self) -> list[SessionRef]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.mtime, reverse=True)

    def get_session_ref(self, sid: str) -> SessionRef | None:
        return self._sessions.get(sid)

    def get_parsed(self, sid: str) -> SessionParsed | None:
        ref = self.get_session_ref(sid)
        if ref is None:
            return None
        try:
            mtime = ref.transcript_path.stat().st_mtime
        except OSError:
            return None
        cached = self._parse_cache.get(sid)
        if cached and cached[0] == mtime:
            return cached[1]
        parsed = parse_transcript(ref.transcript_path)
        self._parse_cache[sid] = (mtime, parsed)
        ref.mtime = mtime
        return parsed

    # ---------------- pub/sub for live updates ----------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, event: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        for q in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

    # public alias so other modules (the API layer) can push events too
    def broadcast(self, event: dict) -> None:
        self._broadcast(event)

    # ---------------- filesystem watcher ----------------
    def start_watcher(self) -> None:
        if self._observer is not None:
            return
        observer = Observer()
        handler = _Handler(self)
        seen_roots: set[Path] = set()
        for ws in self._workspaces.values():
            tdir = ws.storage_dir / "GitHub.copilot-chat" / "transcripts"
            if tdir.is_dir() and tdir not in seen_roots:
                observer.schedule(handler, str(tdir), recursive=False)
                seen_roots.add(tdir)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def stop_watcher(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    # ---------------- live activity ticker ----------------
    def collect_activity(self) -> list[dict]:
        """Return a snapshot of every in-progress session's current activity (hidden ones are filtered out)."""
        hidden_sids, hidden_wss = hidden_mod.get_hide_sets()
        out: list[dict] = []
        for ref in self.sessions():
            if ref.session_id in hidden_sids or ref.workspace.hash in hidden_wss:
                continue
            parsed = self.get_parsed(ref.session_id)
            if parsed is None or not parsed.in_progress:
                continue
            out.append({
                "session_id": parsed.session_id,
                "workspace": {
                    "hash": ref.workspace.hash,
                    "label": ref.workspace.label,
                    "short": ref.workspace.short,
                },
                "state": parsed.activity_state,
                "label": parsed.activity_label,
                "since": parsed.activity_since,
                "detail": parsed.activity_detail[:400],
                "tool_calls": parsed.tool_calls,
                "last_event_at": parsed.last_event_at,
            })
        return out

    async def _ticker(self, period: float = 2.0) -> None:
        while True:
            try:
                now = time.time()
                snapshot = self.collect_activity()
                alerts_list = self.collect_alerts(now=now)
                self._broadcast({
                    "type": "activity.tick",
                    "items": snapshot,
                    "alerts": alerts_list,
                    "now": now,
                })
            except Exception:
                pass
            await asyncio.sleep(period)

    def start_ticker(self) -> None:
        if self._loop is None or self._tick_task is not None:
            return
        self._tick_task = self._loop.create_task(self._ticker())

    def stop_ticker(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None

    def collect_alerts(self, now: float | None = None, limit: int = 200) -> list[dict]:
        """Walk every session and aggregate alerts, sorted by severity then recency.

        Hidden sessions / workspaces are excluded — muting in the UI also mutes alerts.
        """
        if now is None:
            now = time.time()
        sets = hidden_mod.get_sets()
        hidden_sids, hidden_wss = sets["sessions"], sets["workspaces"]
        dismissed = sets["dismissed_alerts"]
        out: list[dict] = []
        for ref in self.sessions():
            if ref.session_id in hidden_sids or ref.workspace.hash in hidden_wss:
                continue
            parsed = self.get_parsed(ref.session_id)
            if parsed is None:
                continue
            for a in alerts_mod.detect(parsed, now=now):
                if a["id"] in dismissed:
                    continue
                a["workspace"] = {
                    "hash": ref.workspace.hash,
                    "label": ref.workspace.label,
                    "short": ref.workspace.short,
                }
                # Use last user message as a hint to identify the session.
                a["session_label"] = (parsed.to_summary().get("first_user_message") or parsed.session_id)[:120]
                a["in_progress"] = parsed.in_progress
                out.append(a)
        out.sort(key=alerts_mod.sort_key)
        return out[:limit]

    # called by handler when a transcript file changed
    def _on_transcript_change(self, path: Path) -> None:
        sid = path.stem
        # invalidate parse cache; ensure session is indexed
        self._parse_cache.pop(sid, None)
        ref = self._sessions.get(sid)
        if ref is None:
            # new session — refresh index
            self.refresh_index()
            ref = self._sessions.get(sid)
        else:
            try:
                ref.mtime = path.stat().st_mtime
            except OSError:
                pass
        self._broadcast({"type": "session.updated", "session_id": sid})


class _Handler(FileSystemEventHandler):
    def __init__(self, store: Store) -> None:
        self.store = store

    def _maybe(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix != ".jsonl":
            return
        self.store._on_transcript_change(p)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe(event)
