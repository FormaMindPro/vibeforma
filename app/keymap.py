"""Canonical hotkey combo tokens, shared by the listener and the wizard.

No pynput here on purpose: app/hotkey.py imports pynput at module level to
install the listener, while app/onboarding.py must import cleanly without a
keyboard backend. Both read the normalization from this module, so a combo
typed by hand is normalized the same way on either path.
"""

# pynput names the left/right variants; a combo names the modifier once.
SPECIAL_KEY_TOKENS = {
    "alt_gr": "alt",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
}

# Spellings a person types that are not the pynput key names: the parser only
# takes the canonical name, so "control" and "win" have to become "ctrl" and
# "cmd" before a combo reaches it.
KEY_ALIASES = {
    "control": "ctrl",
    "command": "cmd",
    "win": "cmd",
    "super": "cmd",
    "meta": "cmd",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "ins": "insert",
    "pageup": "page_up",
    "pagedown": "page_down",
}

# The canonical modifier tokens; the order is also the display order.
MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")
MODIFIERS = frozenset(MODIFIER_ORDER)


def normalize_key(token: object) -> str:
    """One lowercase combo token: aliases mapped, '<ctrl>' unwrapped."""
    text = str(token or "").strip().strip("<>").lower()
    if not text:
        return ""
    if len(text) == 1:
        return text
    text = SPECIAL_KEY_TOKENS.get(text, text)
    return KEY_ALIASES.get(text, text)


def combo_tokens(combo: str) -> list[str]:
    """'Ctrl+Alt+Space' -> ['ctrl', 'alt', 'space']; empty tokens dropped."""
    tokens = [normalize_key(raw) for raw in str(combo or "").split("+")]
    return [token for token in tokens if token]


def combo_to_pynput(combo: str) -> str:
    """'control+alt+space' -> '<ctrl>+<alt>+<space>', what HotKey.parse takes."""
    return "+".join(f"<{token}>" for token in combo_tokens(combo))
