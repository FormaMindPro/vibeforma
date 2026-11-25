"""External hook and voice actions: the user's commands on the recognized text.

The hook sends the final text to the user's command on stdin. Off while the
command is empty (the JSON default). The command runs without a shell, so
metacharacters in it are plain characters; the text is UTF-8 on stdin. A
failure or a timeout is logged as a status only — never the text — and
never breaks the ordinary insertion.

Voice actions bind a whole phrase to a command, to another model or to an
external agent. The list lives in config.json under "actions" and is
matched after the dictionary and the snippets, so a snippet may stand in
for the trigger. Matching is exact for the whole phrase: an action never
fires from inside an ordinary sentence.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from typing import Any

HOOK_DEFAULT_TIMEOUT_S = 5.0
HOOK_MIN_TIMEOUT_S = 1.0
HOOK_MAX_TIMEOUT_S = 30.0

ACTION_KINDS = ("command", "model", "agent")
MAX_ACTIONS = 200
# Trimmed from both ends of a phrase before an action is compared: the ASR
# may add a period or wrap the phrase in quotes (unicode escapes, ASCII source).
_PHRASE_EDGES = (" \t\r\n\"'`.,!?;:()[]{}<>"
                 "\u2013\u2014\u2026\u00ab\u00bb")


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


# -- voice actions ---------------------------------------------------------
def _phrase_key(text: Any) -> str:
    """The phrase as compared for an action: case, spacing and outer edges."""
    folded = " ".join(str(text or "").split()).casefold()
    return folded.strip(_PHRASE_EDGES)


def _action(entry: Any) -> dict[str, str] | None:
    """One raw entry as {"trigger", "kind", ...} or None when unusable.

    "command" runs the configured hook_command unless the entry names its
    own; "agent" runs the entry's own command (an external agent); "model"
    names the model to switch to once the dictation ends.
    """
    if not isinstance(entry, dict):
        return None
    trigger = " ".join(str(entry.get("trigger") or "").split())
    if not _phrase_key(trigger):
        return None
    kind = str(entry.get("kind") or "command").strip().lower()
    if kind not in ACTION_KINDS:
        return None
    action = {"trigger": trigger, "kind": kind}
    if kind == "model":
        model = str(entry.get("model") or "").strip()
        if not model:
            return None
        action["model"] = model
        return action
    command = str(entry.get("command") or "").strip()
    if kind == "agent" and not command:
        return None
    if command:
        action["command"] = command
    return action


def normalize_actions(entries: Any) -> list[dict[str, str]]:
    """Clean, de-duplicate (case-insensitive trigger) and cap the action list."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries or []:
        action = _action(item)
        if action is None:
            continue
        key = _phrase_key(action["trigger"])
        if key in seen:
            continue
        seen.add(key)
        out.append(action)
        if len(out) >= MAX_ACTIONS:
            break
    return out


def match_action(text: str, entries: Any) -> dict[str, str] | None:
    """The action the whole phrase stands for, or None.

    The phrase is compared exactly after casefolding, whitespace collapsing
    and trimming the outer punctuation, so an action never fires from inside
    an ordinary sentence. The service calls this after the dictionary and the
    snippets, so a snippet may expand into a trigger.
    """
    wanted = _phrase_key(text)
    if not wanted:
        return None
    for action in normalize_actions(entries):
        if _phrase_key(action["trigger"]) == wanted:
            return action
    return None

