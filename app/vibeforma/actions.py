"""Dictionary, history, undo and the tray quick actions of the service."""
from __future__ import annotations

import threading
import time
from typing import Any

import history
from config import LANGS, save
from config import input_device_names as device_names

from . import STATE_ERROR, STATE_IDLE, STATE_PAUSED, STATE_RECORDING
from .base import ServiceHost


class HistoryActions(ServiceHost):
    # -- dictionary / history ----------------------------------------------
    def _remember(self, text: str, way: str = history.WAY_INSERT) -> None:
        """Write the dictation to the local history when the user opted in."""
        if not self.cfg.get("history", False):
            return
        app = self._target_title if self.cfg.get("history_app", False) else ""
        try:
            self.history.add(text, app=app, way=way)
        except Exception as e:  # noqa: BLE001 - a broken history file must not break dictation
            self.log.warning("History write failed: %s", e)

    def history_records(self) -> list[Any]:
        records: list[Any] = self.history.load()
        return records

    def history_delete(self, index: int) -> None:
        self.history.delete(index)

    def history_clear(self) -> None:
        self.history.clear()

    def history_copy(self, text: str, hint: str = "history.copied") -> None:
        import insert as insert_mod
        insert_mod.copy_text(text or "")
        message = self.tr(hint)
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)

    def undo_last(self) -> str:
        """Ctrl+Z in the same window within 30 s; otherwise a warning."""
        import insert as insert_mod
        code = str(history.undo_allowed(self._last_insert, time.time(),
                                        insert_mod.get_foreground_hwnd()))
        if code == "ok":
            insert_mod.send_undo()
            self._last_insert = None
            message = self.tr("history.undo_done")
        else:
            message = self.tr(f"history.undo_{code}")
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        return message

    def restore_last_clipboard(self) -> str:
        """Put the clipboard back as it was before the last insertion."""
        import insert as insert_mod
        state = self._last_insert or {}
        if not state.get("prev_known"):
            return self.tr("history.restore_none")
        insert_mod.copy_text(state.get("prev") or "")
        message = self.tr("history.restored")
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        return message

    # -- tray actions -----------------------------------------------------
    def toggle_pause(self) -> None:
        if self.state == STATE_PAUSED:
            self._set_state(STATE_IDLE)
        elif self.state in (STATE_IDLE, STATE_ERROR):
            self._set_state(STATE_PAUSED)
        elif self.tray:
            self.tray.notify(self.tr("notify.wait_cycle"))

    def input_device_names(self) -> list[Any]:
        return list(device_names())

    def is_current_device(self, name: str) -> bool:
        current = str(self.cfg.get("input_device") or "").lower()
        return name.lower().find(current) >= 0

    def set_device_by_name(self, name: str) -> None:
        if self.state == STATE_RECORDING:
            if self.tray:
                self.tray.notify(self.tr("notify.stop_before_device"))
            return
        self.cfg["input_device"] = name
        save(self.cfg)
        if self.tray:
            self.tray.notify(self.tr("notify.device_set", name=name))
        self.log.info("Input device changed: %s", name)

    def set_language(self, code: str) -> None:
        """Recognition language from the tray quick menu."""
        if code not in LANGS:
            return
        self.cfg["language"] = code
        save(self.cfg)
        if self.tray:
            self.tray.notify(self.tr("notify.lang_set", name=code))
        self.log.info("Recognition language: %s", code)

    def current_model(self) -> str:
        return str(self.cfg["model"])

    # -- onboarding ---------------------------------------------------------
    def open_onboarding(self) -> None:
        """Tray/HUD request: the HUD (main thread) opens the wizard."""
        self.live["open_onboarding"] = True

    def finish_onboarding(self, choices: dict[str, Any]) -> None:
        """Apply the wizard's answers (an empty dict means Skip: keep safe)."""
        import onboarding
        merged = onboarding.apply_choices(
            self.cfg,
            language=choices.get("language"),
            locale=choices.get("locale"),
            hotkey=choices.get("hotkey"),
            input_device=choices.get("input_device"))
        new_cfg = dict(self.cfg)
        for key in ("language", "locale", "hotkey", "input_device",
                    "onboarding_done"):
            new_cfg[key] = merged[key]
        self.apply_settings(new_cfg)
        self.log.info("Onboarding finished: language=%s hotkey=%s device=%s",
                      merged["language"], merged["hotkey"],
                      merged["input_device"])

    def start_mic_test(self) -> bool:
        """Short recording for the wizard: transcribe, show, never insert."""
        if self.state != STATE_IDLE:
            return False
        self.live["test_text"] = ""
        self.live["test_state"] = "recording"
        self._test_mode = True
        self._panel_mode = False
        self.start_recording()
        if self._test_mode and self.state == STATE_RECORDING:
            import onboarding
            timer = threading.Timer(onboarding.TEST_SECONDS,
                                    self._mic_test_timeout)
            timer.daemon = True
            timer.start()
            return True
        self._finish_mic_test("", "error")
        return False

    def _mic_test_timeout(self) -> None:
        if self._test_mode and self.state == STATE_RECORDING:
            self.stop_recording("test")

    def stop_mic_test(self) -> None:
        if self._test_mode and self.state == STATE_RECORDING:
            self.stop_recording("test")

    def _finish_mic_test(self, text: str, state: str | None = None) -> None:
        self.live["test_text"] = text or ""
        self.live["test_state"] = state or ("done" if text else "empty")
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self._test_mode = False
