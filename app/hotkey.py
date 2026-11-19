"""Global hotkey via pynput; the state machine lives in the service."""
import logging

from pynput import keyboard


def combo_to_pynput(combo: str) -> str:
    """'ctrl+alt+space' -> '<ctrl>+<alt>+<space>'."""
    return "+".join("<%s>" % t.strip().lower() for t in combo.split("+") if t.strip())


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
        except Exception:
            self._log.exception("Hotkey handler error")
