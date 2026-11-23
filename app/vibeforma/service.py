"""The dictation service: recording, recognition, insertion into the field."""
from __future__ import annotations

import contextlib
import os
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
        self.file_jobs = None
        self.pipe: Any = None
        self._last_file_outputs: list[str] = []
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
            "file_job": {},
            "file_batch": {},
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
        jobs = getattr(self, "file_jobs", None)
        if jobs is not None:
            # A dictation outranks any file job: between segments the job
            # pauses and resumes once the service is idle again.
            jobs.dictation_active(
                state in (STATE_RECORDING, STATE_PROCESSING))
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
            source = str(self.cfg.get("audio_source") or "microphone")
            kind = "system" if source == "system" else "mic"
            if kind == "system":
                self.log.exception("System audio unavailable: %s", e)
            else:
                self.log.exception("Microphone unavailable: %s", e)
            if self._test_mode:
                self._finish_mic_test("", "error")
            self._set_state(STATE_IDLE)
            if self.tray:
                self.tray.notify(errors.text(self.cfg, kind, e))
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
            target_hwnd = getattr(self, "_target_hwnd", None)
            if (target_hwnd and self.cfg.get("confirm_focus_change", True)
                    and insert_mod.get_foreground_hwnd() != target_hwnd):
                # Dictation started in one window, the text is ready in
                # another: never a silent paste and never a dialog — the text
                # waits in the panel draft.
                self.log.info("Target window changed — draft in the panel")
                self._show_draft(text)
                return
            undo_state: dict[str, Any] = {}
            result = insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=target_hwnd,
                paste_method=self.cfg.get("paste_method", "shift_insert"),
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

    # -- file mode ---------------------------------------------------------
    def start_pipe(self) -> bool:
        """Start the CLI channel before the model load.

        The pipe must answer during loading, when ``location.json`` already
        names the running service: a caller gets a defined "loading" reply
        instead of "VibeForma is not running". Returns whether it listens.
        """
        if self.pipe is not None:
            return True
        try:
            from .pipe import PipeServer
            server = PipeServer(self._pipe_request, self.log)
            if server.start():
                self.pipe = server
                self.log.info("CLI pipe listening: %s", server.name)
                return True
            self.log.info("CLI pipe: not available on this platform")
        except Exception as e:  # noqa: BLE001 - the pipe must not stop dictation
            self.log.warning("CLI pipe not started: %s", e)
        self.pipe = None
        return False

    def start_file_mode(self) -> None:
        """Create the job queue once the model is ready; the pipe is older."""
        if self.file_jobs is not None:
            return
        from file_jobs import FileJobs
        self.file_jobs = FileJobs(self.cfg, self.asr, self.log,
                                  on_event=self._on_file_event,
                                  on_batch_event=self._on_batch_event)

    def _on_file_event(self, job: Any) -> None:
        """One live view for the HUD and the tray, never the transcript text."""
        payload = {
            "id": job.id,
            "batch_id": job.batch_id,
            "state": job.state,
            "progress": round(float(job.progress), 1),
            "segments": len(job.segments),
            "name": os.path.basename(job.source),
            "relative": job.relative,
            "error": job.error or "",
            "finished_at": float(job.finished_at or 0.0),
        }
        previous = self.live.get("file_job") or {}
        previous_id = str(previous.get("id") or "")
        previous_batch = str(previous.get("batch_id") or "")
        previous_state = str(previous.get("state") or "")
        previous_live_single = (bool(previous_id) and not previous_batch
                                and previous_state in
                                ("queued", "running", "paused"))
        same = previous_id == job.id
        if job.batch_id:
            # A batch child never overwrites a live single-file slot: the
            # batch snapshot carries the current name.
            take = same or not previous_id or bool(previous_batch)
        else:
            take = (same or not previous_id or bool(previous_batch)
                    or not previous_live_single
                    or job.state in ("queued", "running", "paused"))
        if take:
            self.live["file_job"] = payload
        if job.outputs:
            self._last_file_outputs = list(job.outputs)
        # A batch child reports through the batch: one notification, not one
        # per file, so a folder of twenty files is not twenty popups.
        if not job.batch_id and job.state in ("done", "error", "cancelled"):
            if job.state == "done" and job.outputs:
                self.set_last(self.tr("notify.file_done",
                                      path=job.outputs[0]))
                if self.tray:
                    self.tray.notify(self.tr("notify.file_done",
                                             path=job.outputs[0]))
            elif job.state == "error":
                self.set_last(self.tr("notify.file_failed",
                                      error=job.error or ""))
                if self.tray:
                    self.tray.notify(self.tr("notify.file_failed",
                                             error=job.error or ""))
        if self.tray:
            with contextlib.suppress(Exception):
                self.tray.set_state(self.state)

    def _on_batch_event(self, batch: Any) -> None:
        """The batch snapshot the HUD panel reads; one notice at the end."""
        previous = self.live.get("file_batch") or {}
        counts = {"total": int(batch.total or 0), "done": 0, "skipped": 0,
                  "failed": 0, "cancelled": 0, "pending": 0}
        current_progress = 0.0
        jobs = getattr(self, "file_jobs", None)
        if jobs is not None:
            with contextlib.suppress(Exception):
                counts = jobs.counts(batch)
            if batch.current:
                with contextlib.suppress(Exception):
                    current = jobs.get(batch.current)
                    if current is not None:
                        current_progress = float(current.progress)
        payload = {
            "id": batch.id,
            "batch_id": batch.id,
            "state": batch.state,
            "total": int(counts["total"]),
            "done": int(counts["done"]),
            "skipped": int(counts["skipped"]),
            "failed": int(counts["failed"]),
            "cancelled": int(counts["cancelled"]),
            "pending": int(counts["pending"]),
            "progress": round(float(batch.progress), 1),
            "current": batch.current,
            "current_name": batch.current_name,
            "current_progress": round(current_progress, 1),
            "error": batch.error,
            "finished_at": float(batch.finished_at or 0.0),
        }
        ended = batch.state in ("done", "cancelled")
        was_ended = (previous.get("id") == batch.id
                     and previous.get("state") in ("done", "cancelled"))

        def _rank(state: str) -> int:
            if state in ("running", "paused"):
                return 2
            if state == "queued":
                return 1
            return 0

        same_id = previous.get("id") == batch.id
        incoming_rank = _rank(batch.state)
        previous_rank = _rank(str(previous.get("state") or ""))
        take = (same_id or not previous.get("id")
                or incoming_rank > previous_rank)
        if not take and incoming_rank == previous_rank == 0:
            take = (float(payload["finished_at"])
                    >= float(previous.get("finished_at") or 0.0))
        if take:
            self.live["file_batch"] = payload
        if ended and not was_ended:
            if batch.cancelled:
                message = self.tr("notify.batch_cancelled")
            elif counts["failed"]:
                message = self.tr("notify.batch_failed",
                                  done=counts["done"],
                                  skipped=counts["skipped"],
                                  failed=counts["failed"])
            else:
                message = self.tr("notify.batch_done", done=counts["done"],
                                  skipped=counts["skipped"],
                                  failed=counts["failed"])
            self.set_last(message)
            if self.tray:
                self.tray.notify(message)

    def has_active_batch(self) -> bool:
        jobs = self.file_jobs
        if jobs is None:
            return False
        with contextlib.suppress(Exception):
            return bool(jobs.active_batches())
        return False

    def cancel_batch(self) -> int:
        """Tray: stop every unfinished batch; the number actually stopped."""
        jobs = self.file_jobs
        if jobs is None:
            return 0
        with contextlib.suppress(Exception):
            return int(jobs.cancel_batches())
        return 0

    def transcribe_file_dialog(self) -> None:
        """Tray: the person picks a file, then it joins the queue.

        The dialog runs in its own thread with its own Tk root, so the HUD
        loop and the hotkey stay free; the person's choice is consent and is
        not bound by ``file_roots``.
        """
        threading.Thread(target=self._transcribe_file_dialog_worker,
                         daemon=True).start()

    def _transcribe_file_dialog_worker(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            from file_jobs import SUPPORTED_MEDIA
            media = " ".join("*" + ext for ext in SUPPORTED_MEDIA)
            root = tk.Tk()
            root.withdraw()
            try:
                path = filedialog.askopenfilename(
                    title=self.tr("dialog.transcribe_file"),
                    filetypes=[
                        (self.tr("dialog.media"), media),
                        (self.tr("dialog.all_files"), "*.*"),
                    ])
            finally:
                root.destroy()
        except Exception as e:  # noqa: BLE001 - a broken dialog must not kill the HUD
            self.log.warning("File dialog failed: %s", e)
            return
        if not path:
            return
        if self.file_jobs is None:
            self.set_last(self.tr("notify.file_not_ready"))
            if self.tray:
                self.tray.notify(self.tr("notify.file_not_ready"))
            return
        try:
            self.file_jobs.submit(path, language=self.cfg.get("language") or "",
                                  formats=["srt", "txt"], trusted=True)
        except Exception as e:  # noqa: BLE001 - a refused path is a notice
            self.log.warning("File job refused: %s", e)
            self.set_last(self.tr("notify.file_failed", error=str(e)))
            if self.tray:
                self.tray.notify(self.tr("notify.file_failed", error=str(e)))

    def transcribe_folder_dialog(self) -> None:
        """Tray: the person picks a folder, then every media file joins.

        The dialog runs in its own thread with its own Tk root, like the
        single-file one; the person's choice is consent, so ``trusted`` is
        set and ``file_roots`` does not apply. Locality still does.
        """
        threading.Thread(target=self._transcribe_folder_dialog_worker,
                         daemon=True).start()

    def _transcribe_folder_dialog_worker(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                folder = filedialog.askdirectory(
                    mustexist=True,
                    title=self.tr("dialog.transcribe_folder"))
            finally:
                root.destroy()
        except Exception as e:  # noqa: BLE001 - a broken dialog must not kill the HUD
            self.log.warning("Folder dialog failed: %s", e)
            return
        if not folder:
            return
        if self.file_jobs is None:
            self.set_last(self.tr("notify.file_not_ready"))
            if self.tray:
                self.tray.notify(self.tr("notify.file_not_ready"))
            return
        from file_jobs import NoMediaFiles
        try:
            self.file_jobs.submit_folder(
                folder,
                language=self.cfg.get("language") or "",
                formats=["srt", "txt"],
                recursive=bool(self.cfg.get("file_folder_recursive", False)),
                trusted=True)
        except NoMediaFiles:
            self.set_last(self.tr("notify.folder_empty"))
            if self.tray:
                self.tray.notify(self.tr("notify.folder_empty"))
        except Exception as e:  # noqa: BLE001 - a refused folder is a notice
            self.log.warning("Folder job refused: %s", e)
            message = self.tr("notify.folder_failed", error=str(e))
            self.set_last(message)
            if self.tray:
                self.tray.notify(message)

    def last_file_folder(self) -> str:
        """The folder of the last finished job, for the tray's open action."""
        if not self._last_file_outputs:
            return ""
        return os.path.dirname(self._last_file_outputs[0])

    def open_last_file_folder(self) -> None:
        folder = self.last_file_folder()
        if not folder:
            return
        with contextlib.suppress(Exception):
            os.startfile(folder)  # noqa: S606 - the user asked to see the result

    def _pipe_request(self, request: dict[str, Any],
                      send: Any) -> dict[str, Any]:
        """One CLI call over the pipe: submit, stream progress, answer."""
        command = str(request.get("command") or "")
        if command == "transcribe":
            return self._transcribe_file_request(request, send)
        if command == "transcribe_folder":
            return self._transcribe_folder_request(request, send)
        if command == "cancel_batch":
            return self._cancel_batch_request(request)
        return {"event": "error", "ok": False, "code": 2,
                "error": "unknown command"}

    def _transcribe_file_request(self, request: dict[str, Any],
                                 send: Any) -> dict[str, Any]:
        if self.file_jobs is None:
            return {"event": "error", "ok": False, "code": 1, "loading": True,
                    "error": "the model is loading"}
        formats = [str(f).lower() for f in request.get("formats") or []]
        import asr as asr_mod
        from file_jobs import FilePathError
        try:
            job = self.file_jobs.submit(
                str(request.get("path") or ""),
                language=str(request.get("language") or ""),
                model=str(request.get("model") or ""),
                formats=formats,
                out=str(request.get("out") or ""),
                force=bool(request.get("force")),
            )
        except asr_mod.ModelNotInstalled as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except FilePathError as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except (OSError, ValueError) as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 1, "error": str(e)}
        while True:
            send({"event": "progress", "job_id": job.id, "state": job.state,
                  "progress": round(float(job.progress), 1)})
            if job.state in ("done", "error", "cancelled"):
                break
            time.sleep(0.3)
        if job.state == "done":
            return {"event": "done", "ok": True, "job_id": job.id,
                    "out": list(job.outputs),
                    "segments": len(job.segments),
                    "duration_s": round(float(job.duration), 3)}
        if job.state == "cancelled":
            return {"event": "error", "ok": False, "cancelled": True,
                    "job_id": job.id, "error": "cancelled"}
        return {"event": "error", "ok": False, "job_id": job.id,
                "error": job.error or job.state}

    def _transcribe_folder_request(self, request: dict[str, Any],
                                   send: Any) -> dict[str, Any]:
        """Submit one folder, stream the batch counters, answer with a summary."""
        if self.file_jobs is None:
            return {"event": "error", "ok": False, "code": 1, "loading": True,
                    "error": "the model is loading"}
        import asr as asr_mod
        from file_jobs import FilePathError, NoMediaFiles
        formats = [str(f).lower() for f in request.get("formats") or []]
        try:
            batch = self.file_jobs.submit_folder(
                str(request.get("folder") or ""),
                language=str(request.get("language") or ""),
                formats=formats,
                out=str(request.get("out") or ""),
                recursive=bool(request.get("recursive")),
                force=bool(request.get("force")),
            )
        except NoMediaFiles as e:
            self.log.warning("CLI folder job refused: %s", e)
            return {"event": "error", "ok": False, "code": 4,
                    "error": str(e) or "nothing to transcribe"}
        except (asr_mod.ModelNotInstalled, FilePathError) as e:
            self.log.warning("CLI folder job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except (OSError, ValueError) as e:
            self.log.warning("CLI folder job refused: %s", e)
            return {"event": "error", "ok": False, "code": 1, "error": str(e)}
        last: tuple[Any, ...] | None = None
        while True:
            counts = self.file_jobs.counts(batch)
            snapshot = (batch.state, counts["done"], counts["skipped"],
                        counts["failed"], counts["cancelled"],
                        int(round(float(batch.progress))))
            if snapshot != last:
                last = snapshot
                file_progress = 0.0
                if batch.current:
                    with contextlib.suppress(Exception):
                        current = self.file_jobs.get(batch.current)
                        if current is not None:
                            file_progress = float(current.progress)
                send({"event": "progress", "batch_id": batch.id,
                      "state": batch.state, "total": counts["total"],
                      "done": counts["done"], "skipped": counts["skipped"],
                      "failed": counts["failed"],
                      "cancelled": counts["cancelled"],
                      "pending": counts["pending"],
                      "progress": round(float(batch.progress), 1),
                      "current": batch.current,
                      "file": batch.current_name,
                      "file_progress": round(file_progress, 1)})
            if batch.state in ("done", "cancelled"):
                break
            time.sleep(0.3)
        summary = self._batch_summary(batch)
        if batch.cancelled:
            return {"event": "error", "ok": False, "cancelled": True,
                    "batch_id": batch.id, "code": 3, "error": "cancelled",
                    "summary": summary}
        return {"event": "done", "ok": summary["ok"], "batch_id": batch.id,
                "summary": summary}

    def _batch_summary(self, batch: Any) -> dict[str, Any]:
        """The JSON summary the CLI prints: every file, in walk order."""
        jobs = self.file_jobs
        counts = {"total": int(batch.total or 0), "done": 0, "skipped": 0,
                  "failed": 0, "cancelled": 0, "pending": 0}
        if jobs is not None:
            with contextlib.suppress(Exception):
                counts = jobs.counts(batch)
        files: list[dict[str, Any]] = []
        for child_id in batch.children:
            job = None
            if jobs is not None:
                with contextlib.suppress(Exception):
                    job = jobs.get(child_id)
            if job is None:
                continue
            files.append({
                "source": job.source,
                "relative": job.relative,
                "state": job.state,
                "outputs": list(job.outputs),
                "segments": len(job.segments),
                "duration_s": round(float(job.duration), 3),
                "error": job.error,
            })
        return {
            "ok": counts["failed"] == 0 and counts["cancelled"] == 0,
            "batch_id": batch.id,
            "folder": batch.root,
            "out": batch.out,
            "recursive": bool(batch.recursive),
            "total": int(counts["total"]),
            "done": int(counts["done"]),
            "skipped": int(counts["skipped"]),
            "failed": int(counts["failed"]),
            "cancelled": int(counts["cancelled"]),
            "progress": round(float(batch.progress), 1),
            "files": files,
        }

    def _cancel_batch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.file_jobs is None:
            return {"ok": False, "cancelled": 0, "code": 1,
                    "error": "the model is loading"}
        batch_id = str(request.get("batch_id") or "")
        if batch_id:
            stopped = 1 if self.file_jobs.cancel_batch(batch_id) else 0
        else:
            stopped = self.file_jobs.cancel_batches()
        return {"ok": True, "cancelled": int(stopped)}

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
                self.start_file_mode()
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
        if self.pipe is not None:
            with contextlib.suppress(Exception):
                self.pipe.stop()
        if self.file_jobs is not None:
            with contextlib.suppress(Exception):
                self.file_jobs.shutdown()
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
