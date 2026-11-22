"""External hook: the recognized text goes to the user's command on stdin.

Off while the command is empty (the JSON default). The command runs without a
shell, so metacharacters in it are plain characters; the text is UTF-8 on
stdin. A failure or a timeout is logged as a status only — never the text —
and never breaks the ordinary insertion.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from typing import Any

HOOK_DEFAULT_TIMEOUT_S = 5.0
HOOK_MIN_TIMEOUT_S = 1.0
HOOK_MAX_TIMEOUT_S = 30.0


def clamp_timeout(value: Any) -> float:
    """The hook timeout kept inside 1-30 s; a non-number falls back to 5."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = HOOK_DEFAULT_TIMEOUT_S
    return min(HOOK_MAX_TIMEOUT_S, max(HOOK_MIN_TIMEOUT_S, seconds))


def split_command(command: str) -> list[str]:
    """Split the user's command line without a shell.

    posix=False keeps Windows backslashes intact; the quotes around a path
    with spaces are removed here, since no shell is left to remove them.
    """
    tokens = shlex.split(command or "", posix=False)
    return [_unquote(token) for token in tokens]


def _unquote(token: str) -> str:
    if len(token) > 1 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _no_window_flag() -> int:
    """Keep a console from flashing when the service has no console."""
    return 0x08000000 if sys.platform == "win32" else 0


def run_hook(command: str, text: str, timeout_s: float, log: Any) -> bool:
    """Run the hook; True when the command exited with code 0.

    The command receives the text on stdin and prints wherever it wants;
    its output is discarded. Nothing from ``text`` may reach the log.
    """
    argv = split_command(command)
    if not argv:
        return False
    timeout = clamp_timeout(timeout_s)
    try:
        # The command is the user's own setting, never the dictated text.
        proc = subprocess.run(  # noqa: S603 - shell=False, argv from shlex
            argv,
            input=str(text or "").encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=_no_window_flag())
    except subprocess.TimeoutExpired:
        log.warning("Hook timed out after %.0f s", timeout)
        return False
    except (OSError, ValueError) as e:
        log.warning("Hook could not start: %s", e)
        return False
    if proc.returncode != 0:
        log.warning("Hook exit code: %s", proc.returncode)
        return False
    log.info("Hook exit code: 0")
    return True
