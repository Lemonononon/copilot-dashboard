"""User-facing view-state registry (hide / archive / pin / dismissed alerts).

The dashboard never modifies or deletes Copilot's transcript files. Everything
stored here is purely a local "view layer" preference, kept under:

    ~/.config/copilot-dashboard/hidden.json     (Linux / XDG)
    ~/Library/Application Support/copilot-dashboard/hidden.json  (macOS)
    %APPDATA%/copilot-dashboard/hidden.json     (Windows)

Schema (v2):
    {
      "version": 2,
      "sessions":          [<sessionId>, ...],   # hidden (muted) sessions
      "workspaces":        [<workspaceHash>, ...],
      "archived_sessions": [<sessionId>, ...],   # archived but not deleted
      "pinned_sessions":   [<sessionId>, ...],   # float to top
      "dismissed_alerts":  [<alertId>, ...]      # alerts the user dismissed
    }

Backwards compatible with v1 (only `sessions` / `workspaces` keys).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path


_LOCK = threading.Lock()

_KEYS_LIST = (
    "sessions",
    "workspaces",
    "archived_sessions",
    "pinned_sessions",
    "dismissed_alerts",
)


def _config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "copilot-dashboard"


def _path() -> Path:
    return _config_dir() / "hidden.json"


def _empty() -> dict:
    return {k: [] for k in _KEYS_LIST} | {"version": 2}


def _load_unlocked() -> dict:
    p = _path()
    if not p.is_file():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        out = _empty()
        for k in _KEYS_LIST:
            v = data.get(k) or []
            out[k] = [str(x) for x in v if x]
        return out
    except Exception:
        return _empty()


def _save_unlocked(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def load() -> dict:
    with _LOCK:
        return _load_unlocked()


def get_sets() -> dict:
    """Return all five collections as sets, for fast membership checks."""
    d = load()
    return {k: set(d[k]) for k in _KEYS_LIST}


# Backwards-compat shim for callers that still expect the old (sessions, workspaces)
# tuple from the v1 module.
def get_hide_sets() -> tuple[set[str], set[str]]:
    s = get_sets()
    return s["sessions"], s["workspaces"]


def _bucket_for(kind: str) -> str:
    mapping = {
        "session": "sessions",
        "workspace": "workspaces",
        "archive": "archived_sessions",
        "pin": "pinned_sessions",
        "alert": "dismissed_alerts",
    }
    if kind not in mapping:
        raise ValueError(f"unknown kind: {kind!r}")
    return mapping[kind]


def set_one(kind: str, ident: str, on: bool) -> dict:
    """Toggle membership of a single id in a single bucket; return registry."""
    if not ident:
        raise ValueError("ident is required")
    bucket = _bucket_for(kind)
    with _LOCK:
        data = _load_unlocked()
        cur = set(data[bucket])
        if on:
            cur.add(ident)
        else:
            cur.discard(ident)
        data[bucket] = sorted(cur)
        _save_unlocked(data)
        return data


def set_many(kind: str, ids: list[str], on: bool) -> dict:
    """Bulk toggle: add or remove a list of ids in a single bucket."""
    bucket = _bucket_for(kind)
    ids = [str(i) for i in ids if i]
    with _LOCK:
        data = _load_unlocked()
        cur = set(data[bucket])
        if on:
            cur |= set(ids)
        else:
            cur -= set(ids)
        data[bucket] = sorted(cur)
        _save_unlocked(data)
        return data
