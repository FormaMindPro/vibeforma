"""status.json: the running instance's state, read by ``--status``.

The file is a convenience for scripts, not a source of truth: the service
writes it on every state change, a failed write never reaches the recorder.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any

from config import DATA_DIR, VERSION

STATUS_MAX_AGE_S = 60.0   # a --status answer older than this is not an answer


def status_path() -> str:
    """Where the service publishes its state."""
    return os.path.join(DATA_DIR, "status.json")


def write_status(path: str, state: str, version: str = VERSION,
                 pid: int | None = None, now: float | None = None) -> bool:
    """Write the state atomically; a failure is reported, never raised.

    The temporary file keeps a reader from seeing a half-written JSON; the
    replace is atomic on Windows and POSIX alike.
    """
    payload = {
        "state": str(state),
        "version": str(version),
        "pid": os.getpid() if pid is None else int(pid),
        "ts": time.time() if now is None else float(now),
    }
    tmp = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False
    return True


def read_status(path: str, max_age_s: float = STATUS_MAX_AGE_S,
                now: float | None = None) -> dict[str, Any] | None:
    """The published state when it is well-formed and fresh, None otherwise."""
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return None
        state = payload.get("state")
        ts = float(payload.get("ts") or 0)
        int(payload.get("pid") or 0)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(state, str) or not state.strip():
        return None
    moment = time.time() if now is None else float(now)
    # A timestamp from the future is as untrustworthy as a stale one.
    if abs(moment - ts) > max_age_s:
        return None
    return payload
