"""The typing surface shared by every part of the service.

The Service class is assembled from small mixins; each of them reads the
attributes declared here and the core fills them in ``Service.__init__``.
Only the pieces every part needs live here.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any


class ServiceHost:
    cfg: dict[str, Any]
    log: Any
    state: str
    tray: Any
    hotkey: Any
    http_server: Any
    asr: Any
    vad: Any
    recorder: Any
    live: dict[str, Any]
    history: Any
    stats: Any
    pending_update: dict[str, Any] | None
    update_check_state: str
    update_check_tag: str
    _state_lock: threading.Lock
    _running: bool
    _partial_busy: bool
    _update_busy: bool
    _test_mode: bool
    _panel_mode: bool
    _draft_busy: bool
    _history_cursor: int | None
    _history_saved: str
    _history_total: int
    _last_insert: dict[str, Any] | None
    _record_started: float
    _record_seconds: float
    _target_hwnd: int | None
    _target_title: str

    if TYPE_CHECKING:
        # Implemented by the core and by the update part of Service.
        def _set_state(self, state: str) -> None: ...

        def apply_pending_update(self) -> bool: ...

        def check_updates(self, notify_latest: bool = False) -> None: ...

        def apply_settings(self, new_cfg: dict[str, Any]) -> None: ...

        def start_recording(self) -> None: ...

        def stop_recording(self, reason: str) -> None: ...

        def on_hotkey(self) -> None: ...

        def on_hotkey_press(self) -> None: ...

        def on_hotkey_release(self) -> None: ...

        def shutdown(self) -> None: ...

    def tr(self, key: str, **kw: Any) -> str:
        """Localized UI string for the current locale config."""
        import i18n
        return str(i18n.t(self.cfg, key, **kw))

    def log_path(self) -> str:
        from config import LOG_PATH
        return str(LOG_PATH)

    def set_last(self, message: str) -> None:
        """Panel status line; the HUD hides it after its own time-to-live."""
        self.live["last"] = message
        self.live["last_ts"] = time.time()
