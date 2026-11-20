"""The dictation service: recording, recognition, insertion into the field."""
from __future__ import annotations

import threading
import time
from typing import Any

import dictionary
import errors
import history
from config import SERVICE_DIR, find_input_device

from . import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_UPDATING,
)
from .actions import HistoryActions
from .settings_ops import SettingsOps
from .update_ops import UpdateActions
from .util import beep


class Service(HistoryActions, SettingsOps, UpdateActions):
    def __init__(self, cfg: dict[str, Any], log: Any) -> None:
        self.cfg = cfg
        self.log = log
        self.state = STATE_LOADING
        self._state_lock = threading.Lock()
        self.tray = None
        self.hotkey = None
        self.asr = None
        self.vad = None
        self.recorder = None
        self._running = True
        self._partial_busy = False
        self._update_busy = False
        self._test_mode = False
        self.pending_update = None
        self.restart_pending = None
        self.history = history.History(history.history_path(SERVICE_DIR))
        self._last_insert = None
        self._target_title = ""
        self._target_hwnd = None
        # shared live view for the HUD (written by worker threads, read by GUI)
        self.live: dict[str, Any] = {
            "state": STATE_LOADING,
            "level": 0.0,
            "vad": 0.0,
            "partial": "",
            "last": "",
            "last_ts": 0.0,
            "device": "",
            "exit": False,
            "privacy": bool(self.cfg.get("hud_privacy", False)),
            "open_onboarding": False,
            "open_settings": False,
            "settings_tab": None,
            "test_text": "",
            "test_state": "",
        }

    # -- wiring --------------------------------------------------------
    def attach_tray(self, tray: Any) -> None:
        self.tray = tray

    def attach_hotkey(self, hotkey: Any) -> None:
        self.hotkey = hotkey

    # -- state ---------------------------------------------------------
    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self.state = state
        self.live["state"] = state
        if self.tray:
            self.tray.set_state(state)
        if (state == STATE_IDLE and self.pending_update
                and self.cfg.get("auto_update") == "auto"
                and not self._update_busy):
            threading.Thread(target=self.apply_pending_update,
                             daemon=True).start()

    # -- hotkey ---------------------------------------------------------
    def on_hotkey(self) -> None:
        st = self.state
        if st == STATE_IDLE:
            self.start_recording()
        elif st == STATE_RECORDING:
            self.stop_recording("manual")
        elif st == STATE_PROCESSING:
            self.log.info("Hotkey during recognition — ignored")
        elif st == STATE_PAUSED:
            self.log.info("Service is paused — hotkey ignored")
        elif st == STATE_UPDATING:
            self.log.info("Update in progress — hotkey ignored")
        else:  # loading / error
            self.log.info("Model is not ready yet — hotkey ignored")

    # -- recording -------------------------------------------------------
    def start_recording(self) -> None:
        if self.cfg.get("beep_on"):
            beep(1200, 70)
        dev_idx = find_input_device(self.cfg["input_device"])
        try:
            import sounddevice as sd
            dev_name = str(sd.query_devices(dev_idx)["name"])[:40]
        except Exception as e:  # noqa: BLE001 - PortAudio has no stable base exception
            self.log.debug("Input device name: %s", e)
            dev_name = str(self.cfg.get("input_device", ""))
        self.live["device"] = dev_name
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self._partial_busy = False
        import insert as insert_mod
        self._target_hwnd = insert_mod.get_foreground_hwnd()
        title_wanted = (self.cfg.get("log_window_titles", False)
                        or self.cfg.get("history_app", False))
        self._target_title = insert_mod.foreground_window() if title_wanted else ""
        if self.cfg.get("log_window_titles", False):
            self.log.info("Paste target window: %s (hwnd=%s)",
                          self._target_title, self._target_hwnd)
        else:
            self.log.info("Paste target window: hwnd=%s", self._target_hwnd)
        self.recorder = self._make_recorder()
        try:
            self.recorder.start(dev_idx)
        except Exception as e:
            self.log.exception("Microphone unavailable: %s", e)
            if self._test_mode:
                self._finish_mic_test("", "error")
            self._set_state(STATE_IDLE)
            if self.tray:
                self.tray.notify(errors.text(self.cfg, "mic", e))
            return
        self._set_state(STATE_RECORDING)

    def _make_recorder(self) -> Any:
        import audio as audio_mod
        return audio_mod.Recorder(self.cfg, self.log, self.vad,
                                  self._on_auto_stop,
                                  on_partial=self._on_partial,
                                  live_state=self.live)

    def _on_partial(self, audio_snapshot: Any) -> None:
        """Streaming partial: transcribe the in-flight phrase for the HUD."""
        if self._partial_busy or self.state != STATE_RECORDING or self.asr is None:
            return
        self._partial_busy = True

        def _run() -> None:
            try:
                text = self.asr.transcribe(audio_snapshot, verbose=False)
                if text and self.state == STATE_RECORDING:
                    self.live["partial"] = text
            except Exception as e:  # noqa: BLE001 - a failed partial must not stop the recording
                self.log.debug("Partial transcription: %s", e)
            finally:
                self._partial_busy = False
        threading.Thread(target=_run, daemon=True).start()

    def _on_auto_stop(self, reason: str) -> None:
        if self.state == STATE_RECORDING:
            self.stop_recording(reason)

    def stop_recording(self, reason: str) -> None:
        rec, self.recorder = self.recorder, None
        if rec is None:
            self._set_state(STATE_IDLE)
            return
        buf = rec.stop()
        if self.cfg.get("beep_off"):
            beep(900, 70)
        self._set_state(STATE_PROCESSING)
        threading.Thread(target=self._transcribe_worker, args=(buf, rec),
                         daemon=True).start()

    def _transcribe_worker(self, buf: Any, rec: Any) -> None:
        try:
            if buf is None or not rec.had_speech():
                if self._test_mode:
                    self._finish_mic_test("", "empty")
                    return
                self.log.info("No speech captured — nothing inserted")
                self.set_last(self.tr("notify.no_speech_last"))
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.no_speech"))
                return
            t0 = time.time()
            text = self.asr.transcribe(buf)
            dt = time.time() - t0
            self.log.info("Latency to ready text: %.2f s", dt)
            text, hits = dictionary.apply(text, self.cfg.get("dictionary"))
            if hits:
                self.log.info("Dictionary: %d replacement(s)", hits)
            if self._test_mode:
                # Onboarding check: show the text, never insert it.
                self.log.info("Microphone test: %d chars (not inserted)",
                              len(text or ""))
                self._finish_mic_test(text)
                return
            if not text:
                self.log.info("Empty transcription — nothing inserted")
                self.set_last(self.tr("notify.not_recognized"))
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.not_recognized_short"))
                return
            import insert as insert_mod
            undo_state: dict[str, Any] = {}
            result = insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=getattr(self, "_target_hwnd", None),
                paste_method=self.cfg.get("paste_method", "shift_insert"),
                confirm_focus=(lambda: insert_mod.ask_focus_change(self.cfg))
                if self.cfg.get("confirm_focus_change", True) else None,
                exclude_history=self.cfg.get("clipboard_exclude_history",
                                             True),
                log_title=self.cfg.get("log_window_titles", False),
                undo_state=undo_state)
            self.live["partial"] = ""
            self.live["level"] = 0.0
            if result == insert_mod.RESULT_COPIED:
                self.set_last(self.tr("notify.copied"))
                if self.tray:
                    self.tray.notify(self.tr("notify.copied"))
            elif result == insert_mod.RESULT_CANCELLED:
                self.set_last(self.tr("notify.cancelled"))
            else:
                self._last_insert = {
                    "text": text,
                    "hwnd": getattr(self, "_target_hwnd", None),
                    "ts": time.time(),
                    "prev": undo_state.get("prev"),
                    "prev_known": bool(undo_state.get("prev_known")),
                }
                self._remember(text)
                message = self.inserted_message(text)
                self.set_last(message)
                if self.tray:
                    self.tray.notify(message)
        except Exception as e:
            self.log.exception("Recognition error: %s", e)
            beep(400, 200)
            self.set_last(errors.last_line(self.cfg, "recognition", e))
            if self.tray:
                self.tray.notify(errors.text(self.cfg, "recognition", e))
        finally:
            if self._running:
                self._set_state(STATE_IDLE)

    def inserted_message(self, text: str) -> str:
        """Notification text: the character count, the phrase on opt-in."""
        if self.cfg.get("notify_text", False):
            short = text[:80] + ("…" if len(text) > 80 else "")
            return self.tr("notify.inserted", text=short)
        return self.tr("notify.inserted_chars", n=len(text))

    # -- startup -----------------------------------------------------------
    def load_model_async(self) -> None:
        def _load() -> None:
            try:
                import asr as asr_mod
                from faster_whisper.vad import get_vad_model
                self.asr = asr_mod.ASR(self.cfg, self.log)
                self.asr.load()
                self.log.info("Loading Silero VAD…")
                self.vad = get_vad_model()
                self.log.info("Warm-up (short empty transcription)…")
                import numpy as np
                self.asr.transcribe(np.zeros((16000,), dtype=np.float32))
                self.log.info("Warm-up done — service ready")
                self._set_state(STATE_IDLE)
                if self.tray:
                    self.tray.notify(self.tr(
                        "notify.ready",
                        hotkey=self.cfg["hotkey"].replace("+", "+").upper()))
            except Exception as e:
                self.log.exception("Model failed to load: %s", e)
                self._set_state(STATE_ERROR)
                if self.tray:
                    self.tray.notify(errors.text(self.cfg, "model", e))
        threading.Thread(target=_load, daemon=True).start()

    def shutdown(self) -> None:
        self._running = False
        self.live["exit"] = True
        if self.hotkey:
            try:
                self.hotkey.stop()
            except Exception as e:  # noqa: BLE001 - listener teardown raises engine-specific errors
                self.log.debug("Hotkey stop: %s", e)
        if self.tray:
            self.tray.stop()
