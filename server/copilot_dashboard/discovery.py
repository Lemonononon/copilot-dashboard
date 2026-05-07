"""Discover VS Code Copilot Chat data on the local filesystem.

Layout (Linux):
    ~/.config/Code/User/workspaceStorage/<wsHash>/
        workspace.json                       # {"folder": "file:///..."} OR {"workspace": "..."}
        GitHub.copilot-chat/
            transcripts/<sessionId>.jsonl    # per-conversation event stream
            debug-logs/<sessionId>/main.jsonl

macOS:  ~/Library/Application Support/Code/User/workspaceStorage/...
Win:    %APPDATA%/Code/User/workspaceStorage/...

Code-Insiders/VSCodium variants are also probed.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


def _candidate_user_dirs() -> list[Path]:
    # Explicit override (used by Docker / non-standard installs). May be a
    # ':'-separated list of either workspaceStorage dirs or "User" dirs;
    # we accept both and resolve to workspaceStorage.
    override = os.environ.get("CD_VSCODE_USER_DIR")
    if override:
        out: list[Path] = []
        for raw in override.split(os.pathsep):
            p = Path(raw).expanduser()
            if p.name == "workspaceStorage" and p.is_dir():
                out.append(p)
            elif (p / "workspaceStorage").is_dir():
                out.append(p / "workspaceStorage")
            elif (p / "User" / "workspaceStorage").is_dir():
                out.append(p / "User" / "workspaceStorage")
        if out:
            return out
        # fall through if override yielded nothing usable

    home = Path.home()
    if sys.platform == "darwin":
        bases = [home / "Library" / "Application Support"]
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        bases = [Path(appdata)] if appdata else []
    else:
        bases = [home / ".config"]
    candidates: list[Path] = []
    for base in bases:
        for app in ("Code", "Code - Insiders", "VSCodium", "VSCodium - Insiders"):
            d = base / app / "User" / "workspaceStorage"
            if d.is_dir():
                candidates.append(d)
    return candidates


@dataclass
class WorkspaceInfo:
    hash: str
    storage_dir: Path
    folder: str | None = None      # file:///... single folder
    workspace_file: str | None = None  # multi-root .code-workspace path

    @property
    def label(self) -> str:
        if self.folder:
            return self.folder.replace("file://", "")
        if self.workspace_file:
            return self.workspace_file.replace("file://", "") + " (workspace)"
        return f"<unknown:{self.hash[:8]}>"

    @property
    def short(self) -> str:
        lab = self.label
        return lab.split("/")[-1] or lab


@dataclass
class SessionRef:
    session_id: str
    workspace: WorkspaceInfo
    transcript_path: Path
    debug_log_path: Path | None = None
    mtime: float = 0.0
    size: int = 0


def list_workspaces(roots: list[Path] | None = None) -> list[WorkspaceInfo]:
    roots = roots if roots is not None else _candidate_user_dirs()
    out: list[WorkspaceInfo] = []
    for root in roots:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            ws = WorkspaceInfo(hash=child.name, storage_dir=child)
            wj = child / "workspace.json"
            if wj.is_file():
                try:
                    data = json.loads(wj.read_text(encoding="utf-8"))
                    ws.folder = data.get("folder")
                    ws.workspace_file = data.get("workspace") or data.get("configuration")
                except Exception:
                    pass
            chat_dir = child / "GitHub.copilot-chat"
            if chat_dir.is_dir():
                out.append(ws)
    return out


def list_sessions(workspaces: list[WorkspaceInfo] | None = None) -> list[SessionRef]:
    """Return all sessions across workspaces, sorted by mtime desc."""
    workspaces = workspaces if workspaces is not None else list_workspaces()
    sessions: list[SessionRef] = []
    for ws in workspaces:
        tdir = ws.storage_dir / "GitHub.copilot-chat" / "transcripts"
        if not tdir.is_dir():
            continue
        for f in tdir.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            sid = f.stem
            dbg = ws.storage_dir / "GitHub.copilot-chat" / "debug-logs" / sid / "main.jsonl"
            sessions.append(SessionRef(
                session_id=sid,
                workspace=ws,
                transcript_path=f,
                debug_log_path=dbg if dbg.is_file() else None,
                mtime=st.st_mtime,
                size=st.st_size,
            ))
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield records from a jsonl file, skipping malformed lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return
