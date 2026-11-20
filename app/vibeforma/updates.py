"""Background release check."""
from __future__ import annotations

import threading
import time
from typing import Any

from . import STATE_IDLE
from .base import ServiceHost


def start_update_checker(cfg: dict[str, Any], svc: ServiceHost,
                         log: Any) -> None:
    """First pass shortly after startup, then every update_interval_h hours
    (the settings checkbox turns it off)."""
    def _loop() -> None:
        time.sleep(90)
        while True:
            try:
                if cfg.get("update_check", True) and svc.state == STATE_IDLE:
                    svc.check_updates()
            except Exception as e:  # noqa: BLE001 - one failed check must not stop the loop
                log.debug("Update check: %s", e)
            time.sleep(max(1, float(cfg.get("update_interval_h", 2))) * 3600)
    threading.Thread(target=_loop, daemon=True).start()
