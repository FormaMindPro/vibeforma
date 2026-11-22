"""The dictation service: recording, recognition, insertion into the field."""
from __future__ import annotations

import threading
import time
from typing import Any

import dictionary
import errors
import history
import snippets
import stats
import textrules
from config import DATA_DIR, VERSION, find_input_device

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
from .status import write_status
from .update_ops import UpdateActions
from .util import beep


class Service(HistoryActions, SettingsOps, UpdateActions):
    def __init__(self, cfg: dict[str, Any], log: Any, *,
                 status_file: str | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.state = STATE_LOADING
        self._state_lock = threading.Lock()
        # The CLI reads this file; tests and embedders leave it unset.
        self.status_file = status_file
        self.tray = None
        self.hotkey = None
        self.cancel_watcher = None
        self.asr = None
        self.vad = None
        self.recorder = None
        self.http_server = None
        self._running = True
        self._partial_busy = False
        self._cancel_evt = threading.Event()
        self._update_busy = False
        self._test_mode = False
        self._panel_mode = False
        self._draft_busy = False
        self._history_cursor = None
        self._history_saved = ""
        self._history_total = 0
        self.pending_update = None
        self.panel_request = threading.Event()
        self.history = history.History(history.history_path(DATA_DIR))
        self.stats = stats.Stats(stats.stats_path(DATA_DIR))
        self._record_started = 0.0
        self._record_seconds = 0.0
        self._last_insert = None
        self._target_title = ""
        self._target_hwnd = None
        # shared live view for the HUD (written by worker threads, read by GUI)
        self.live: dict[str, Any] = {
            "state": STATE_LOADING,
            "level": 0.0,
            "vad": 0.0,
            "partial": "",
            "draft": "",
            "draft_rev": 0,
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
        self.publish_status()

    # -- wiring --------------------------------------------------------
    def attach_tray(self, tray: Any) -> None:
        self.tray = tray

    def attach_hotkey(self, hotkey: Any) -> None:
        self.hotkey = hotkey

    def attach_cancel_watcher(self, watcher: Any) -> None:
        self.cancel_watcher = watcher

    def request_panel(self) -> None:
        """Ask the HUD to show itself; safe from any thread."""
        self.panel_request.set()

    # -- state ---------------------------------------------------------
    def publish_status(self, state: str | None = None) -> None:
        """Write status.json for the CLI; a failed write never breaks us."""
        status_file = getattr(self, "status_file", None)
        if not status_file:
            return
        if not write_status(status_file, state or self.state, VERSION):
            self.log.debug("status.json not written: %s", status_file)

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self.state = state
        self.live["state"] = state
        self.publish_status(state)
        self._sync_cancel_watcher(state)
        if self.tray:
            self.tray.set_state(state)
        if (state == STATE_IDLE and self.pending_update
                and self.cfg.get("auto_update") == "auto"
                and not self._update_busy):
            threading.Thread(target=self.apply_pending_update,
                             daemon=True).start()

    def _sync_cancel_watcher(self, state: str) -> None:
        """The global Esc binding lives only while a dictation is in flight.

        Recognition still takes Esc: the user can drop a result already in
        the worker. At rest the listener is gone, so Esc stays with the
        foreground application.
        """
        watcher = self.cancel_watcher
        if watcher is None:
            return
        try:
            if state in (STATE_RECORDING, STATE_PROCESSING):
                watcher.start()
            else:
                watcher.stop()
        except Exception as e:  # noqa: BLE001 - a failed listener must not break dictation
            self.log.debug("Esc watcher: %s", e)

    # -- hotkey ---------------------------------------------------------
    def on_hotkey(self) -> None:
        """Toggle mode only: a single click opens or closes a phrase.

        In hold-to-talk the app wires HoldHotkey to on_hotkey_press and
        on_hotkey_release instead; the CLI toggle still lands here.
        """
        st = self.state
        if st == STATE_IDLE:
            # If the panel owns the foreground, a new phrase belongs to the
            # panel: it clears the history/draft and shows the new text there.
            import insert as insert_mod
            self._panel_mode = bool(insert_mod.is_own_window(
                insert_mod.get_foreground_hwnd()))
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

    def on_hotkey_press(self) -> None:
        """Hold-to-talk: the combo went down, start a phrase from rest.

        Auto-repeat never reaches this method: ComboTracker emits the down
        event once per hold, so a held combo cannot start a second phrase.
        """
        if self.state != STATE_IDLE:
            self.log.info("Hold hotkey press ignored: %s", self.state)
            return
        import insert as insert_mod
        self._panel_mode = bool(insert_mod.is_own_window(
            insert_mod.get_foreground_hwnd()))
        self.start_recording()

    def on_hotkey_release(self) -> None:
        """Hold-to-talk: the combo came up, finish the phrase in flight."""
        if self.state != STATE_RECORDING:
            return
        self.stop_recording("hold")

    # -- panel dictation -------------------------------------------------
    def on_panel_mic(self) -> None:
        """Mic button on the HUD: record, then show the text in the panel.

        A click on the borderless panel leaves no text field to paste into,
        so the recognized text waits in the panel until the user reviews
        it and picks Insert, Edit or Cancel.
        """
        st = self.state
        if st == STATE_IDLE and not self._draft_busy:
            self._panel_mode = True
            self.start_recording()
        elif st == STATE_RECORDING:
            self.stop_recording("panel")
        elif st in (STATE_PROCESSING, STATE_UPDATING):
            self.log.info("Mic button ignored while busy: %s", st)
        else:
            self.log.info("Mic button ignored — the model is not ready: %s", st)

    # -- recording -------------------------------------------------------
    def start_recording(self) -> None:
        self._forget_draft()
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
        self._cancel_evt.clear()
        if self._panel_mode:
            # The panel is the foreground window; there is no field to aim at.
            self._target_hwnd = None
            self._target_title = ""
        else:
            import insert as insert_mod
            self._target_hwnd = insert_mod.get_foreground_hwnd()
            title_wanted = (self.cfg.get("log_window_titles", False)
                            or self.cfg.get("history_app", False))
            self._target_title = (insert_mod.foreground_window()
                                  if title_wanted else "")
            if self.cfg.get("log_window_titles", False):
                self.log.info("Paste target window: %s (hwnd=%s)",
                              self._target_title, self._target_hwnd)
            else:
                self.log.info("Paste target window: hwnd=%s", self._target_hwnd)
        self.recorder = self._make_recorder()
        self._record_started = time.time()
        try:
            self.recorder.start(dev_idx)
        except Exception as e:
            self._record_started = 0.0
            self._record_seconds = 0.0
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
        started = self._record_started
        self._record_started = 0.0
        self._record_seconds = (max(0.0, time.time() - started)
                                if started else 0.0)
        if self.cfg.get("beep_off"):
            beep(900, 70)
        self._set_state(STATE_PROCESSING)
        threading.Thread(target=self._transcribe_worker, args=(buf, rec),
                         daemon=True).start()

    def cancel_recording(self) -> None:
        """Esc: drop the in-flight dictation, nothing is inserted or saved."""
        st = self.state
        if st == STATE_RECORDING:
            rec, self.recorder = self.recorder, None
            if rec is not None:
                try:
                    rec.stop()
                except Exception as e:  # noqa: BLE001 - a dying stream must not block the cancel
                    self.log.debug("Cancelled recorder stop: %s", e)
            self._record_started = 0.0
            self._record_seconds = 0.0
            self._panel_mode = False
            self.live["partial"] = ""
            self.live["level"] = 0.0
            self._set_state(STATE_IDLE)
            self._report_cancelled()
            self.log.info("Recording cancelled — buffer dropped")
        elif st == STATE_PROCESSING:
            if self._cancel_evt.is_set():
                return
            self._cancel_evt.set()
            self.live["partial"] = ""
            self.live["level"] = 0.0
            self._report_cancelled()
            self.log.info("Recognition cancel requested")
        else:
            self.cancel_draft()

    def _report_cancelled(self) -> None:
        message = self.tr("notify.cancelled_recording")
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)

    def _discard_cancelled(self) -> None:
        """Drop a cancelled recognition: no insertion, history or draft."""
        self._cancel_evt.clear()
        self._panel_mode = False
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self.log.info("Recognition cancelled — result dropped")

    def _transcribe_worker(self, buf: Any, rec: Any) -> None:
        try:
            if self._cancel_evt.is_set():
                self._discard_cancelled()
                return
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
            text, rule_hits = textrules.apply(text, self.cfg)
            if rule_hits:
                self.log.info("Text rules: %d change(s)", rule_hits)
            if self._cancel_evt.is_set():
                self._discard_cancelled()
                return
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
            self._count_dictation(text)
            # Run snippets (card-181) here, after the text rules and before
            # the draft and insertion paths, so both receive their result.
            text, snippet_hits = snippets.apply(text,
                                                self.cfg.get("snippets"))
            if snippet_hits:
                self.log.info("Snippets: %d replacement(s)", snippet_hits)
            if self._panel_mode:
                self._panel_mode = False
                self._show_draft(text)
                return
            import insert as insert_mod
            undo_state: dict[str, Any] = {}
            result = insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=getattr(self, "_target_hwnd", None),
                paste_method=self.cfg.get("paste_method", "shift_insert"),
                confirm_focus=(lambda: insert_mod.ask_focus_change(self.cfg, text))
                if self.cfg.get("confirm_focus_change", True) else None,
                exclude_history=self.cfg.get("clipboard_exclude_history",
                                             True),
                log_title=self.cfg.get("log_window_titles", False),
                undo_state=undo_state)
            self.live["partial"] = ""
            self.live["level"] = 0.0
            if result == insert_mod.RESULT_COPIED:
                self._remember(text, way=history.WAY_COPY)
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
                self._run_hook(text)
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
            self._cancel_evt.clear()
            if self._running:
                self._set_state(STATE_IDLE)

    def _count_dictation(self, text: str) -> None:
        """Add one finished phrase to the local counters, once per dictation.

        Runs after the dictionary and the text rules and before the draft
        and insertion branches, so both count once. A mic test, a cancel and
        an empty result return earlier. A broken stats file must never break
        the dictation.
        """
        store = getattr(self, "stats", None)
        if store is None:
            return
        seconds = float(getattr(self, "_record_seconds", 0.0) or 0.0)
        self._record_seconds = 0.0
        try:
            store.record(chars=len(text), words=len(text.split()),
                         seconds=seconds)
        except Exception as e:  # noqa: BLE001 - the counters must not break dictation
            self.log.warning("Stats write failed: %s", e)

    def _run_hook(self, text: str) -> None:
        """Hand the final text to the user's command in a daemon thread.

        Off while hook_command is empty. Runs after the insertion or the
        draft is already in place, so a slow or failing hook never delays
        what the user sees.
        """
        command = str(self.cfg.get("hook_command") or "").strip()
        if not command:
            return
        import hook as hook_mod
        threading.Thread(
            target=hook_mod.run_hook,
            args=(command, text, self.cfg.get("hook_timeout_s", 5), self.log),
            daemon=True).start()

    def inserted_message(self, text: str) -> str:
        """Notification text: the character count, the phrase on opt-in."""
        if self.cfg.get("notify_text", False):
            short = text[:80] + ("…" if len(text) > 80 else "")
            return self.tr("notify.inserted", text=short)
        return self.tr("notify.inserted_chars", n=len(text))

    # -- panel draft -------------------------------------------------------
    def _show_draft(self, text: str) -> None:
        """Hand the recognized text to the panel for review and insertion."""
        self._exit_history()
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self.live["draft"] = text
        self._touch_draft()
        self.set_last(self.tr("hud.draft_ready"))
        self.log.info("Panel draft ready: %d chars", len(text))
        self._run_hook(text)

    def _touch_draft(self) -> None:
        """Change the revision so the HUD re-reads the draft."""
        self.live["draft_rev"] = int(self.live.get("draft_rev") or 0) + 1

    def _forget_draft(self) -> None:
        self._exit_history()
        self.live["draft"] = ""
        self._touch_draft()

    def cancel_draft(self) -> None:
        """Drop the panel draft; a history flip returns the set-aside draft."""
        if self._history_cursor is not None:
            saved = self._history_saved
            self._exit_history()
            if saved:
                self.live["draft"] = saved
                self._touch_draft()
                self.set_last(self.tr("hud.draft_ready"))
                return
        if not self.live.get("draft"):
            return
        self._forget_draft()
        self.set_last(self.tr("notify.draft_cancelled"))
        self.log.info("Panel draft cancelled")

    # -- history browsing ---------------------------------------------------
    def browse_history(self, step: int) -> None:
        """Flip through saved dictations: -1 older, +1 newer.

        The first flip sets the live draft aside; "newer" past the newest
        record brings it back. Insert / Edit / Copy / Cancel work on the
        shown record like on a fresh draft.
        """
        if self.state in (STATE_RECORDING, STATE_PROCESSING):
            self.log.info("History browsing during a dictation — ignored")
            return
        if self._draft_busy:
            self.log.info("History browsing during an insert — ignored")
            return
        if not self.cfg.get("history", False):
            self.set_last(self.tr("history.off"))
            return
        records = self.history_records()
        if not records:
            self.set_last(self.tr("history.empty"))
            return
        newest = list(reversed(records))
        if self._history_cursor is None:
            if step > 0:
                self.set_last(self.tr("history.at_newest"))
                return
            self._history_saved = str(self.live.get("draft") or "")
            self._history_total = len(newest)
            self._history_cursor = 0
        else:
            nxt = self._history_cursor + (1 if step < 0 else -1)
            if nxt < 0:
                saved = self._history_saved
                self._exit_history()
                if saved:
                    self.live["draft"] = saved
                    self._touch_draft()
                    self.set_last(self.tr("hud.draft_ready"))
                else:
                    self._forget_draft()
                    self.set_last(self.tr("history.closed"))
                return
            if nxt >= len(newest):
                self.set_last(self.tr("history.at_oldest"))
                return
            self._history_cursor = nxt
        record = newest[self._history_cursor]
        self.live["draft"] = str(record.get("text") or "")
        self._touch_draft()
        self.set_last(self.tr("history.browsing",
                              n=self._history_cursor + 1,
                              total=self._history_total))
        self.log.info("History draft: %d of %d", self._history_cursor + 1,
                      self._history_total)

    def _exit_history(self) -> None:
        self._history_cursor = None
        self._history_saved = ""
        self._history_total = 0

    def copy_draft(self, text: str) -> None:
        """Put the reviewed panel text into the clipboard and close it."""
        text = (text or "").strip()
        if not text:
            return
        import insert as insert_mod
        insert_mod.copy_text(text)
        if self.cfg.get("clipboard_exclude_history", True):
            insert_mod.exclude_from_clipboard_history()
        self._remember(text, way=history.WAY_COPY)
        self._forget_draft()
        message = self.tr("history.copied")
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        self.log.info("Panel draft copied: %d chars", len(text))

    def insert_draft(self, text: str, target_hwnd: int | None = None) -> None:
        """Paste the reviewed panel text into the window the user picked.

        The draft as it was recognized is remembered next to the final
        text: the worker learns the person's replacements from the pair.
        """
        text = (text or "").strip()
        if not text or self._draft_busy:
            return
        original = str(self.live.get("draft") or "")
        self._draft_busy = True
        threading.Thread(target=self._insert_draft_worker,
                         args=(text, target_hwnd, original), daemon=True).start()

    def _insert_draft_worker(self, text: str, target_hwnd: int | None,
                             original: str | None = None) -> None:
        import insert as insert_mod
        if original is not None and original.strip() != text:
            self._learn_correction(original, text)
        try:
            if not target_hwnd or not insert_mod.activate_window(target_hwnd):
                insert_mod.copy_text(text)
                self._forget_draft()
                message = self.tr("notify.draft_copied")
                self.set_last(message)
                if self.tray:
                    self.tray.notify(message)
                self.log.info("Panel draft copied: no insertion target")
                return
            title_wanted = (self.cfg.get("log_window_titles", False)
                            or self.cfg.get("history_app", False))
            title = insert_mod.foreground_window() if title_wanted else ""
            undo_state: dict[str, Any] = {}
            result = insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=target_hwnd,
                paste_method=self.cfg.get("paste_method", "shift_insert"),
                confirm_focus=None,   # the user already picked the window
                exclude_history=self.cfg.get("clipboard_exclude_history", True),
                log_title=self.cfg.get("log_window_titles", False),
                undo_state=undo_state)
            if result != insert_mod.RESULT_PASTED:
                self._forget_draft()
                message = self.tr("notify.draft_copied")
                self.set_last(message)
                if self.tray:
                    self.tray.notify(message)
                return
            self._forget_draft()
            self._target_hwnd = target_hwnd
            self._target_title = title or ""
            self._last_insert = {
                "text": text,
                "hwnd": target_hwnd,
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
            self.log.exception("Panel insert error: %s", e)
            self.set_last(errors.last_line(self.cfg, "recognition", e))
            self._touch_draft()   # keep the text in the panel for another try
        finally:
            self._draft_busy = False

    def _learn_correction(self, original: str, edited: str) -> None:
        """Remember the words the person replaced in the draft, locally.

        Only the changed words go to corrections.json; the full text is
        never stored or logged. A broken store or a failed write is a line
        in the log, never a broken insertion.
        """
        try:
            import dictlearn
            pairs = dictlearn.collect_correction(original, edited)
            if pairs:
                self.log.info("Dictionary corrections: %d pair(s)", len(pairs))
        except Exception as e:  # noqa: BLE001 - learning must not break insertion
            self.log.warning("Dictionary corrections: %s", e)

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
        if self.cancel_watcher:
            try:
                self.cancel_watcher.stop()
            except Exception as e:  # noqa: BLE001 - listener teardown raises engine-specific errors
                self.log.debug("Esc watcher stop: %s", e)
        if self.tray:
            self.tray.stop()
