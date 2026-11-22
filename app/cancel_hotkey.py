"""Esc cancels a dictation: a listener that exists only while it is on."""
import logging
import threading

from pynput import keyboard


class EscWatcher:
    """Global Esc listener, switched on only while a dictation is in flight.

    The listener is created in start() and destroyed in stop(), so at rest
    no listener exists and Esc stays with the foreground application.
    """

    def __init__(self, callback, log: logging.Logger):
        self._callback = callback
        self._log = log
        self._lock = threading.Lock()
        self._listener = None

    @property
    def active(self) -> bool:
        return self._listener is not None

    def start(self) -> None:
        with self._lock:
            if self._listener is not None:
                return
            self._listener = keyboard.Listener(on_press=self._on_press)
            self._listener.daemon = True
            self._listener.start()
        self._log.debug("Esc watcher: on")

    def stop(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception as e:  # noqa: BLE001 - teardown raises engine-specific errors
            self._log.debug("Esc watcher stop: %s", e)
        self._log.debug("Esc watcher: off")

    def _on_press(self, key: object) -> None:
        if key != keyboard.Key.esc:
            return
        threading.Thread(target=self._call, daemon=True).start()

    def _call(self) -> None:
        try:
            self._callback()
        except Exception:  # noqa: BLE001 - a handler error must not kill the listener
            self._log.exception("Esc handler error")
