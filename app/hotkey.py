"""Global hotkey via pynput; the state machine lives in the service."""
import logging

from pynput import keyboard

# pynput names the left/right variants; a combo names the modifier once.
_SPECIAL_KEY_TOKENS = {
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

_KEY_ALIASES = {
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


def normalize_key(token: object) -> str:
    """One lowercase combo token: aliases mapped, '<ctrl>' unwrapped."""
    text = str(token or "").strip().strip("<>").lower()
    if not text:
        return ""
    if len(text) == 1:
        return text
    text = _SPECIAL_KEY_TOKENS.get(text, text)
    return _KEY_ALIASES.get(text, text)


def key_token(key: object) -> str:
    """A combo token for a pynput key; a plain token passes through.

    pynput hands over a Key member for named keys and a KeyCode for typed
    characters; only the identity matters here, never the keyboard state.
    """
    if isinstance(key, str):
        return normalize_key(key)
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return normalize_key(name)
    char = getattr(key, "char", None)
    if char and not str(char).isspace():
        return str(char).lower()
    return ""


def parse_combo(combo: str) -> tuple[str, ...]:
    """'Ctrl+Alt+Space' -> ('ctrl', 'alt', 'space'); duplicates dropped."""
    keys: list[str] = []
    for raw in str(combo or "").split("+"):
        token = normalize_key(raw)
        if token and token not in keys:
            keys.append(token)
    return tuple(keys)


class ComboTracker:
    """Pure combo state: key presses in, combo down/up events out.

    No pynput here: the tracker works on tokens, so a test drives it with
    plain strings. Pressing the last missing key of the set opens the combo
    exactly once; auto-repeat sends the same token again and is ignored.
    Releasing any combo key closes it, and a foreign key never changes it.
    """

    def __init__(self, combo: str | list[str] | tuple[str, ...]):
        keys = parse_combo(combo) if isinstance(combo, str) else combo
        self._keys = frozenset(normalize_key(k) for k in keys)
        self._pressed: set[str] = set()
        self._down = False

    @property
    def down(self) -> bool:
        """True while the whole combo is held."""
        return self._down

    @property
    def keys(self) -> frozenset[str]:
        """The tokens the combo is made of."""
        return self._keys

    def press(self, key: object) -> bool:
        """True when this press completed the combo (combo down, once)."""
        token = key_token(key)
        if token not in self._keys or token in self._pressed:
            return False
        self._pressed.add(token)
        if self._down or len(self._pressed) != len(self._keys):
            return False
        self._down = True
        return True

    def release(self, key: object) -> bool:
        """True when releasing this key closed the combo (combo up)."""
        token = key_token(key)
        if token not in self._keys:
            return False
        self._pressed.discard(token)
        if not self._down:
            return False
        self._down = False
        return True

    def reset(self) -> None:
        """Forget held keys without emitting combo up (listener restart)."""
        self._pressed.clear()
        self._down = False


def combo_to_pynput(combo: str) -> str:
    """'ctrl+alt+space' -> '<ctrl>+<alt>+<space>'."""
    return "+".join(f"<{t.strip().lower()}>" for t in combo.split("+") if t.strip())


class Hotkey:
    def __init__(self, combo: str, callback, log: logging.Logger):
        self._combo = combo_to_pynput(combo)
        self._callback = callback
        self._log = log
        self._listener = None

    def start(self):
        self._listener = keyboard.GlobalHotKeys({self._combo: self._on})
        self._listener.daemon = True
        self._listener.start()
        self._log.info("Global hotkey: %s", self._combo)

    def stop(self):
        if self._listener:
            self._listener.stop()

    def _on(self):
        try:
            self._callback()
        except Exception:  # noqa: BLE001 - a handler error is logged and must not kill the listener
            self._log.exception("Hotkey handler error")


class HoldHotkey:
    """Hold-to-talk listener: combo down -> on_press, combo up -> on_release."""

    def __init__(self, combo: str, on_press, on_release, log: logging.Logger):
        self._combo = combo
        self._tracker = ComboTracker(combo)
        self._on_press = on_press
        self._on_release = on_release
        self._log = log
        self._listener = None

    def start(self):
        self._tracker.reset()
        self._listener = keyboard.Listener(on_press=self._down,
                                           on_release=self._up)
        self._listener.daemon = True
        self._listener.start()
        self._log.info("Hold hotkey: %s", parse_combo(self._combo))

    def stop(self):
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:  # noqa: BLE001 - teardown raises engine-specific errors
                self._log.debug("Hold hotkey stop: %s", e)
        self._tracker.reset()

    def _down(self, key: object) -> None:
        if self._tracker.press(key):
            self._fire(self._on_press)

    def _up(self, key: object) -> None:
        if self._tracker.release(key):
            self._fire(self._on_release)

    def _fire(self, callback) -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a handler error is logged and must not kill the listener
            self._log.exception("Hold hotkey handler error")


def make_hotkey(combo: str, hold: bool, on_toggle, on_press, on_release,
                log: logging.Logger):
    """The listener the config asks for: hold when true, toggle otherwise."""
    if hold:
        return HoldHotkey(combo, on_press, on_release, log)
    return Hotkey(combo, on_toggle, log)
