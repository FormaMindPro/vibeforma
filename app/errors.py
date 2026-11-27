"""User-facing error catalog: a code, a plain message, the next action.

The UI never shows a raw exception: the caller logs it and asks this module
for a localized explanation and an action. Codes are stable; the text lives
in locales/*.json under "errors.<code>.text" and "errors.<code>.action".
"""
import i18n

KINDS = ("mic", "model", "recognition", "file", "system")

# Fallback code per kind: what to show when no hint matches.
FALLBACK = {
    "mic": "mic.unavailable",
    "model": "model.load",
    "recognition": "recognition.failed",
    "file": "file.read",
    "system": "system.unavailable",
}

# Order matters: a specific hint wins over a general one.
HINTS = (
    ("system.unavailable", ("soundcard", "loopback", "no default output",
                            "system audio")),
    ("mic.permission", ("permission", "access is denied", "access denied",
                        "unanticipated host", "not allowed")),
    ("mic.busy", ("busy", "in use", "unavailable")),
    ("model.driver", ("driver", "cudnn", "dll")),
    ("model.memory", ("out of memory", "failed to allocate", "memory")),
    ("model.network", ("download", "connection", "network", "http", "url",
                       "timeout", "resolve")),
)


def code_for(kind, exc):
    """Stable code for an exception; never the raw text."""
    if kind not in KINDS:
        kind = "recognition"
    probe = (type(exc).__name__ + " " + str(exc)).lower()
    for code, hints in HINTS:
        if code.split(".", 1)[0] != kind:
            continue
        if any(hint in probe for hint in hints):
            return code
    return FALLBACK[kind]


def text(cfg, kind, exc):
    """Localized message and next action, joined by a newline."""
    code = code_for(kind, exc)
    message = i18n.t(cfg, "errors." + code + ".text")
    action = i18n.t(cfg, "errors." + code + ".action")
    return message + "\n" + action


def last_line(cfg, kind, exc):
    """One-line form for the panel status: the message, no action."""
    return i18n.t(cfg, "errors." + code_for(kind, exc) + ".text")
