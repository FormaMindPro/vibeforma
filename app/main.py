"""VibeForma — local voice dictation service for Windows.

Hotkey -> mic recording -> local recognition (faster-whisper, ~99 languages)
-> text inserted into the focused field (clipboard + Shift+Insert).
Fully local: the network is used only for the first model download.
"""
import contextlib
import ctypes
import sys
import os
import subprocess
import threading
import time

from config import (CONFIG_PATH, LOCK_PATH, LOG_DIR, LOG_PATH,
                    SERVICE_DIR, TEST_WAV, VERSION, find_input_device,
                    input_device_names, load, save)
import dictionary
import errors
import history

STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_PAUSED = "paused"
STATE_ERROR = "error"
STATE_UPDATING = "updating"


# ---------------------------------------------------------------- utilities
def setup_logging(console: bool):
    import logging
    from logging.handlers import RotatingFileHandler

    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger("dictation")
    root.setLevel(logging.INFO)
    if root.handlers:  # idempotent (selftest + service in one process)
        return root
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
    return root


def beep(freq: int, dur_ms: int):
    def _run():
        try:
            import winsound
            winsound.Beep(freq, dur_ms)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


PYTHONW = os.path.join(SERVICE_DIR, ".venv", "Scripts", "pythonw.exe")
MAIN_PY = os.path.join(SERVICE_DIR, "main.py")
AUTOSTART_LNK = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    "VibeForma.lnk")


def ensure_autostart(create: bool):
    """Manage the logon shortcut from the settings checkbox."""
    try:
        if create and not os.path.exists(AUTOSTART_LNK):
            ps = ("$ws=New-Object -ComObject WScript.Shell;"
                  "$l=$ws.CreateShortcut('%s');"
                  "$l.TargetPath='%s';"
                  "$l.Arguments='\"%s\"';"
                  "$l.WorkingDirectory='%s';"
                  "$l.Save()" % (AUTOSTART_LNK, PYTHONW, MAIN_PY, SERVICE_DIR))
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps],
                creationflags=0x08000000, timeout=30, check=False)
            setup_logging(False).info("Autostart shortcut created")
        elif not create and os.path.exists(AUTOSTART_LNK):
            os.remove(AUTOSTART_LNK)
            setup_logging(False).info("Autostart shortcut removed")
    except Exception as e:
        setup_logging(False).warning("Autostart management: %s", e)


class SingleInstance:
    """PID-file lock with exclusive create + stale recovery.

    The second instance exits quietly; a lock left by a dead process
    (crash, power loss) is taken over.
    """

    def __init__(self, path):
        self._path = path
        self._pid = os.getpid()
        try:
            self._take_lock(create=True)
        except FileExistsError:
            pid = self._read_pid()
            if pid and self._alive(pid):
                raise RuntimeError("another instance is already running")
            # stale lock from a dead process: replace it
            try:
                os.remove(path)
            except OSError:
                pass
            self._take_lock(create=True)

    def _take_lock(self, create):
        fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(self._pid).encode("ascii"))
        os.close(fd)

    def _read_pid(self):
        try:
            with open(self._path) as f:
                return int(f.read().strip() or 0)
        except Exception:
            return 0

    @staticmethod
    def _alive(pid):
        if pid <= 0:
            return False
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        kernel32.CloseHandle(h)
        return True

    def release(self):
        try:
            if self._read_pid_static() == self._pid:
                os.remove(self._path)
        except Exception:
            pass

    def _read_pid_static(self):
        try:
            with open(self._path) as f:
                return int(f.read().strip() or 0)
        except Exception:
            return 0


# ---------------------------------------------------------------- service
class Service:
    def __init__(self, cfg, log):
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
        # shared live view for the HUD (written by worker threads, read by GUI)
        self.live = {
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
    def attach_tray(self, tray):
        self.tray = tray

    def attach_hotkey(self, hotkey):
        self.hotkey = hotkey

    def tr(self, key, **kw):
        """Localized UI string for the current locale config."""
        import i18n
        return i18n.t(self.cfg, key, **kw)

    def log_path(self):
        return LOG_PATH

    # -- state ---------------------------------------------------------
    def _set_state(self, state):
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

    def set_last(self, message):
        """Panel status line; the HUD hides it after its own time-to-live."""
        self.live["last"] = message
        self.live["last_ts"] = time.time()

    # -- hotkey ---------------------------------------------------------
    def on_hotkey(self):
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
    def start_recording(self):
        if self.cfg.get("beep_on"):
            beep(1200, 70)
        dev_idx = find_input_device(self.cfg["input_device"])
        try:
            import sounddevice as sd
            dev_name = str(sd.query_devices(dev_idx)["name"])[:40]
        except Exception:
            dev_name = self.cfg.get("input_device", "")
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

    def _make_recorder(self):
        import audio as audio_mod
        return audio_mod.Recorder(self.cfg, self.log, self.vad,
                                  self._on_auto_stop,
                                  on_partial=self._on_partial,
                                  live_state=self.live)

    def _on_partial(self, audio_snapshot):
        """Streaming partial: transcribe the in-flight phrase for the HUD."""
        if self._partial_busy or self.state != STATE_RECORDING or self.asr is None:
            return
        self._partial_busy = True

        def _run():
            try:
                text = self.asr.transcribe(audio_snapshot, verbose=False)
                if text and self.state == STATE_RECORDING:
                    self.live["partial"] = text
            except Exception as e:
                self.log.debug("Partial transcription: %s", e)
            finally:
                self._partial_busy = False
        threading.Thread(target=_run, daemon=True).start()

    def _on_auto_stop(self, reason):
        if self.state == STATE_RECORDING:
            self.stop_recording(reason)

    def stop_recording(self, reason):
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

    def _transcribe_worker(self, buf, rec):
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
            undo_state = {}
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

    def inserted_message(self, text):
        """Notification text: the character count, the phrase on opt-in."""
        if self.cfg.get("notify_text", False):
            short = text[:80] + ("…" if len(text) > 80 else "")
            return self.tr("notify.inserted", text=short)
        return self.tr("notify.inserted_chars", n=len(text))

    # -- dictionary / history ----------------------------------------------
    def _remember(self, text):
        """Write the dictation to the local history when the user opted in."""
        if not self.cfg.get("history", False):
            return
        app = self._target_title if self.cfg.get("history_app", False) else ""
        try:
            self.history.add(text, app=app)
        except Exception as e:  # noqa: BLE001 - a broken file must not break dictation
            self.log.warning("History write failed: %s", e)

    def history_records(self):
        return self.history.load()

    def history_delete(self, index):
        self.history.delete(index)

    def history_clear(self):
        self.history.clear()

    def history_copy(self, text, hint="history.copied"):
        import insert as insert_mod
        insert_mod.copy_text(text or "")
        message = self.tr(hint)
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)

    def undo_last(self):
        """Ctrl+Z in the same window within 30 s; otherwise a warning."""
        import insert as insert_mod
        code = history.undo_allowed(self._last_insert, time.time(),
                                    insert_mod.get_foreground_hwnd())
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

    def restore_last_clipboard(self):
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
    def toggle_pause(self):
        if self.state == STATE_PAUSED:
            self._set_state(STATE_IDLE)
        elif self.state in (STATE_IDLE, STATE_ERROR):
            self._set_state(STATE_PAUSED)
        elif self.tray:
            self.tray.notify(self.tr("notify.wait_cycle"))

    def input_device_names(self):
        return input_device_names()

    def is_current_device(self, name):
        return name.lower().find((self.cfg.get("input_device") or "").lower()) >= 0

    def set_device_by_name(self, name):
        if self.state == STATE_RECORDING:
            if self.tray:
                self.tray.notify(self.tr("notify.stop_before_device"))
            return
        self.cfg["input_device"] = name
        save(self.cfg)
        if self.tray:
            self.tray.notify(self.tr("notify.device_set", name=name))
        self.log.info("Input device changed: %s", name)

    def set_language(self, code):
        """Recognition language from the tray quick menu."""
        from config import LANGS
        if code not in LANGS:
            return
        self.cfg["language"] = code
        save(self.cfg)
        if self.tray:
            self.tray.notify(self.tr("notify.lang_set", name=code))
        self.log.info("Recognition language: %s", code)

    def current_model(self):
        return self.cfg["model"]

    # -- onboarding ---------------------------------------------------------
    def open_onboarding(self):
        """Tray/HUD request: the HUD (main thread) opens the wizard."""
        self.live["open_onboarding"] = True

    def finish_onboarding(self, choices):
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

    def start_mic_test(self):
        """Short recording for the wizard: transcribe, show, never insert."""
        if self.state != STATE_IDLE:
            return False
        self.live["test_text"] = ""
        self.live["test_state"] = "recording"
        self._test_mode = True
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

    def _mic_test_timeout(self):
        if self._test_mode and self.state == STATE_RECORDING:
            self.stop_recording("test")

    def stop_mic_test(self):
        if self._test_mode and self.state == STATE_RECORDING:
            self.stop_recording("test")

    def _finish_mic_test(self, text, state=None):
        self.live["test_text"] = text or ""
        self.live["test_state"] = state or ("done" if text else "empty")
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self._test_mode = False

    # -- settings ----------------------------------------------------------
    def open_settings(self, tab=None):
        """Tray/HUD request: the HUD (main thread) opens the dialog."""
        self.live["settings_tab"] = tab
        self.live["open_settings"] = True

    def apply_settings(self, new_cfg):
        """Save and apply to the live service; called from the HUD thread."""
        old_hotkey = self.cfg.get("hotkey")
        old_dev = self.cfg.get("input_device")
        self.cfg.update(new_cfg)   # same dict object — recorder reads it live
        save(self.cfg)
        self.log.info("Settings saved: silence=%s ms, autostop=%s, "
                      "paste=%s, hotkey=%s, device=%s, language=%s, locale=%s",
                      self.cfg["silence_ms"], self.cfg["auto_stop"],
                      self.cfg["paste_method"], self.cfg["hotkey"],
                      self.cfg["input_device"], self.cfg.get("language"),
                      self.cfg.get("locale"))
        self.set_last(self.tr("notify.saved"))
        ensure_autostart(bool(self.cfg.get("autostart", True)))
        if self.cfg["hotkey"] != old_hotkey and self.hotkey:
            import hotkey as hotkey_mod
            new_hotkey = hotkey_mod.Hotkey(self.cfg["hotkey"],
                                           self.on_hotkey, self.log)
            try:
                # The new listener starts first: a failed start leaves the
                # old hotkey alive instead of leaving none.
                new_hotkey.start()
            except Exception:
                self.log.exception("Hotkey %s did not start - keeping %s",
                                   self.cfg["hotkey"], old_hotkey)
                self.cfg["hotkey"] = old_hotkey
                save(self.cfg)
                if self.tray:
                    self.tray.notify(self.tr("notify.hotkey_failed",
                                             combo=self.cfg["hotkey"]))
            else:
                old_listener, self.hotkey = self.hotkey, new_hotkey
                try:
                    old_listener.stop()
                except Exception:
                    pass
        if self.cfg["input_device"] != old_dev and self.state == STATE_RECORDING:
            if self.tray:
                self.tray.notify(self.tr("notify.device_next"))

    def set_model(self, name):
        if self.state != STATE_IDLE:
            if self.tray:
                self.tray.notify(self.tr("notify.wait_cycle"))
            return
        def _load():
            self._set_state(STATE_LOADING)
            ok = self.asr.reload_model(name)
            save(self.cfg)
            self._set_state(STATE_IDLE if ok else STATE_ERROR)
            if self.tray:
                self.tray.notify(self.tr("notify.model_ok", name=name) if ok
                                 else self.tr("notify.model_failed", name=name))
        threading.Thread(target=_load, daemon=True).start()

    def switch_model(self, new_name, keep_old, progress):
        """Settings-driven switch: download -> load -> (maybe) remove old.

        progress is a plain dict polled by the ModelSwitchDialog on the HUD
        thread (phase/name/detail/cache_note).
        """
        threading.Thread(target=self._switch_model_worker,
                         args=(new_name, keep_old, progress), daemon=True).start()

    def _switch_model_worker(self, new_name, keep_old, progress):
        import os as os_mod
        import shutil
        try:
            if self.state != STATE_IDLE:
                progress.update(phase="busy")
                return
            old = self.cfg["model"]
            if new_name == old:
                progress.update(phase="done", name=new_name)
                return
            import asr as asr_mod
            progress.update(phase="download", name=new_name)
            try:
                asr_mod._resolve_model(new_name, self.log)
            except Exception as e:
                self.log.exception("Model download failed: %s", e)
                progress.update(phase="error",
                                detail=errors.text(self.cfg, "model", e))
                return
            progress.update(phase="load", name=new_name)
            ok = self.asr.reload_model(new_name)
            if not ok:
                self.cfg["model"] = old   # reload_model had already set the new name
                save(self.cfg)
                progress.update(phase="error",
                                detail=errors.text(self.cfg, "model",
                                                   RuntimeError("load failed")))
                return
            save(self.cfg)
            if not keep_old:
                progress.update(phase="remove", name=old)
                from config import MODELS_DIR
                old_dir = os_mod.path.join(MODELS_DIR, old)
                if os_mod.path.isdir(old_dir):
                    shutil.rmtree(old_dir, ignore_errors=True)
                    self.log.info("Old model removed: %s", old_dir)
                else:
                    progress["cache_note"] = old
                    self.log.info("Old model %s lives in the shared HF cache — untouched", old)
            progress.update(phase="done", name=new_name)
            if self.tray:
                self.tray.notify(self.tr("notify.model_ok", name=new_name))
        except Exception as e:
            self.log.exception("Model switch failed: %s", e)
            progress.update(phase="error",
                            detail=errors.text(self.cfg, "model", e))

    # -- updates -----------------------------------------------------------
    def check_updates(self, notify_latest: bool = False):
        """Check GitHub; a newer release only becomes pending (no silent apply)."""
        import updater
        try:
            rel = updater.latest_release()
        except Exception as e:
            self.log.debug("Update check failed: %s", e)
            if notify_latest and self.tray:
                self.tray.notify(self.tr("notify.update_fail"))
            return
        if rel is None:
            if notify_latest and self.tray:
                self.tray.notify(self.tr("notify.update_fail"))
            return
        current = updater.version_tuple(VERSION)
        if not updater.is_newer(rel["version"], current):
            if notify_latest and self.tray:
                self.tray.notify(self.tr("notify.update_latest", ver=VERSION))
            return
        self.log.info("New release %s found (current %s)", rel["tag"], VERSION)
        self.pending_update = rel
        if self.tray:
            self.tray.notify(self.tr("notify.update_found", ver=rel["tag"]))
        if (self.cfg.get("auto_update") == "auto"
                and self.state == STATE_IDLE):
            self.apply_pending_update()

    def update_if_newer(self, notify_latest: bool = False):
        """Backward-compatible alias: a check never installs on its own."""
        self.check_updates(notify_latest)

    def apply_pending_update(self):
        """Install the pending release after consent (tray) or explicit opt-in."""
        rel = self.pending_update
        if not rel or self._update_busy:
            return False
        if self.state != STATE_IDLE:
            if self.tray:
                self.tray.notify(self.tr("notify.update_later", ver=rel["tag"]))
            return False
        self._update_busy = True

        def _work():
            import updater
            self._set_state(STATE_UPDATING)
            if self.tray:
                self.tray.notify(self.tr("notify.update_applying", ver=rel["tag"]))
            ok, reason = updater.apply(rel, SERVICE_DIR, self.log)
            self._update_busy = False
            self.pending_update = None
            self._set_state(STATE_IDLE)
            if ok:
                self.restart_pending = rel["tag"]
                if self.cfg.get("auto_update") == "auto":
                    self._restart_after_update(rel["tag"])
                elif self.tray:
                    self.tray.notify(self.tr(
                        "notify.update_installed", ver=rel["tag"]))
            elif self.tray:
                self.log.warning("Update was not applied: %s", reason)
                self.tray.notify(self.tr("notify.update_refused"))

        threading.Thread(target=_work, daemon=True).start()
        return True

    def dismiss_pending_update(self):
        self.pending_update = None

    def restart_for_update(self):
        tag = self.restart_pending
        self.restart_pending = None
        if tag:
            self._restart_after_update(tag)

    def check_updates_now(self):
        """Settings button: manual check with a visible result."""
        threading.Thread(target=self.check_updates, args=(True,),
                         daemon=True).start()

    def _restart_after_update(self, tag):
        if self.tray:
            self.tray.notify(self.tr("notify.updated_restart", ver=tag))
        self.log.info("Restarting service after update to %s", tag)

        def _spawn():
            time.sleep(2.0)   # let the old window/tray settle
            try:
                # detached pythonw: os.startfile proved a silent no-op in
                # non-interactive contexts, cmd-start flashes a console
                pythonw = os.path.join(SERVICE_DIR, ".venv", "Scripts",
                                       "pythonw.exe")
                subprocess.Popen(
                    [pythonw, os.path.join(SERVICE_DIR, "main.py")],
                    cwd=SERVICE_DIR,
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.log.info("Relaunch spawned: %s", pythonw)
            except Exception:
                self.log.exception("Restart after update failed")

        # non-daemon: the interpreter waits for it, so the relaunch really
        # runs even though shutdown() tears the service down right away
        threading.Thread(target=_spawn, daemon=False).start()
        time.sleep(0.5)
        self.shutdown()

    # -- startup -----------------------------------------------------------
    def load_model_async(self):
        def _load():
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

    def shutdown(self):
        self._running = False
        self.live["exit"] = True
        try:
            if self.hotkey:
                self.hotkey.stop()
        except Exception:
            pass
        if self.tray:
            self.tray.stop()


# ---------------------------------------------------------------- http api
HTTP_MAX_BODY = 10 * 1000 * 1000   # bytes accepted for one request
HTTP_MIN_INTERVAL_S = 1.0          # one request per second
HTTP_TOKEN_HEADER = "X-VibeForma-Token"  # noqa: S105 - a header name


def start_http_api(cfg, svc, log):
    """Optional local API: POST http://127.0.0.1:8787/transcribe (WAV body).

    Off by default. Needs http_token (fail-closed without it), takes only
    audio/wav up to 10 MB, one request per second, works only while the
    service is idle, and always deletes the temporary WAV.
    """
    import hmac as hmac_mod
    import json as json_mod
    import tempfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = str(cfg.get("http_token") or "").strip()
    if not token:
        log.warning("HTTP API is enabled but http_token is empty — refusing "
                    "to start (set http_token in config.json)")
        return None
    last_call = [0.0]

    def set_state(state):
        setter = getattr(svc, "_set_state", None)
        if setter:
            setter(state)
        else:
            svc.state = state

    class Handler(BaseHTTPRequestHandler):
        timeout = 15

        def _reject(self, code, message=None):
            """Answer without the unread body: drain a small one first, so the
            client reads the response instead of a connection reset."""
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if 0 < length <= 1024 * 1024:
                with contextlib.suppress(Exception):
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
            self.send_error(code, message)

        def do_POST(self):
            if self.path != "/transcribe":
                self._reject(404)
                return
            given = self.headers.get(HTTP_TOKEN_HEADER, "")
            if not hmac_mod.compare_digest(given, token):
                self._reject(401, "missing or wrong X-VibeForma-Token")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip().lower() != "audio/wav":
                self._reject(415, "Content-Type must be audio/wav")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length <= 0:
                self._reject(411)
                return
            if length > HTTP_MAX_BODY:
                self._reject(413, "body is larger than 10 MB")
                return
            now = time.monotonic()
            if now - last_call[0] < HTTP_MIN_INTERVAL_S:
                self._reject(429, "one request per second")
                return
            last_call[0] = now
            if svc.state != STATE_IDLE:
                self._reject(409)
                return
            tmp = ""
            try:
                data = self.rfile.read(length)
                fd, tmp = tempfile.mkstemp(prefix="_http_in_", suffix=".wav",
                                           dir=LOG_DIR)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                set_state(STATE_PROCESSING)
                import audio as audio_mod
                audio = audio_mod.load_wav_16k(tmp)
                text = svc.asr.transcribe(audio)
                body = json_mod.dumps({"text": text},
                                      ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:  # noqa: BLE001 - report, never crash the server
                log.exception("HTTP API error: %s", e)
                self.send_error(500)
            finally:
                if tmp:
                    with contextlib.suppress(OSError):
                        os.remove(tmp)
                set_state(STATE_IDLE)

        def log_message(self, fmt, *args):
            log.debug("HTTP: " + fmt % args)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(cfg["http_port"])), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("HTTP API: http://127.0.0.1:%d/transcribe (token required)",
                 cfg["http_port"])
        return server
    except OSError as e:
        log.warning("HTTP API not started: %s", e)
        return None


# ---------------------------------------------------------------- updates
def start_update_checker(cfg, svc, log):
    """Background release check: first pass shortly after startup, then
    every update_interval_h hours (the settings checkbox turns it off)."""
    def _loop():
        time.sleep(90)
        while True:
            try:
                if cfg.get("update_check", True) and svc.state == STATE_IDLE:
                    svc.check_updates()
            except Exception as e:
                log.debug("Update check: %s", e)
            time.sleep(max(1, float(cfg.get("update_interval_h", 2))) * 3600)
    threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------- selftest
def selftest():
    """Machine gate: imports -> model -> test WAV -> text; exit 0/1."""
    log = setup_logging(console=True)
    log.info("=== selftest started ===")
    print("VibeForma", VERSION, "service dir:",
          os.path.dirname(os.path.abspath(__file__)))
    try:
        # (a) imports
        import numpy as np
        import sounddevice
        import pyperclip
        import faster_whisper
        import ctranslate2
        print("imports_ok: faster_whisper=%s ctranslate2=%s sounddevice=%s" %
              (faster_whisper.__version__, ctranslate2.__version__,
               getattr(sounddevice, "__version__", "?")))
        print("cuda_devices:", ctranslate2.get_cuda_device_count())

        cfg = load()
        print("config:", {k: cfg[k] for k in ("model", "device", "compute_type",
                                              "language", "locale", "hotkey",
                                              "input_device")})
        cfg = dict(cfg)
        cfg["language"] = "ru"   # selftest WAV is Russian; pin it for the gate
        print("selftest_language: ru (pinned for the Russian test WAV)")

        # (b) model
        import asr as asr_mod
        engine = asr_mod.ASR(cfg, log)
        t0 = time.time()
        engine.load()
        load_s = time.time() - t0
        print("model_load_s: %.1f" % load_s)

        # (c) test WAV -> text (language auto = per-phrase detection)
        import audio as audio_mod
        if not os.path.exists(TEST_WAV):
            raise RuntimeError("test WAV not found: %s" % TEST_WAV)
        audio = audio_mod.load_wav_16k(TEST_WAV)
        print("test_wav_s: %.1f" % (audio.shape[0] / 16000.0))
        engine.transcribe(np.zeros((16000,), dtype=np.float32))  # warm-up
        t0 = time.time()
        text = engine.transcribe(audio)
        dt = time.time() - t0
        print("transcribe_s: %.2f" % dt)
        print("text:", text)
        has_cyr = any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in text)
        roots = ("тест", "фраз", "реч", "распознав", "локальн")
        hit = sum(1 for r in roots if r in text.lower())
        print("cyrillic:", has_cyr, "keyword_hits:", hit)

        # (d) clipboard round-trip (non-destructive)
        prev = None
        try:
            prev = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy("VF_SELFTEST_MARKER")
        ok_clip = pyperclip.paste() == "VF_SELFTEST_MARKER"
        if prev is not None:
            pyperclip.copy(prev)
        print("clipboard_roundtrip:", ok_clip)

        gate = has_cyr and hit >= 1 and ok_clip
        print("SELFTEST OK" if gate else "SELFTEST FAIL: keyword/cyrillic/clipboard")
        log.info("=== selftest finished: %s ===", "OK" if gate else "FAIL")
        return 0 if gate else 1
    except Exception as e:
        print("SELFTEST FAIL: %s" % e)
        log.exception("selftest crashed: %s", e)
        return 1


# ---------------------------------------------------------------- main
def _enable_dpi_awareness():
    """Native rendering at any Windows scaling (150% default on laptops).

    Without it Tk draws at 96 DPI and Windows stretches the window as a
    bitmap — all text comes out blurry. Must run before the first window.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    if sys.platform == "win32":
        _enable_dpi_awareness()
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    log = setup_logging(console=False)
    try:
        lock = SingleInstance(LOCK_PATH)
    except RuntimeError:
        log.info("Second instance not started — one is already running")
        return
    log.info("=== service starting (pid %d, version %s) ===", os.getpid(), VERSION)

    cfg = load()
    svc = Service(cfg, log)

    import hotkey as hotkey_mod
    import tray as tray_mod
    tray = tray_mod.Tray(svc, log)
    svc.attach_tray(tray)
    hk = hotkey_mod.Hotkey(cfg["hotkey"], svc.on_hotkey, log)
    svc.attach_hotkey(hk)
    hk.start()

    if cfg.get("http_api"):
        start_http_api(cfg, svc, log)

    start_update_checker(cfg, svc, log)

    svc.load_model_async()
    if cfg.get("hud", True):
        import hud as hud_mod
        # Tk lives in its own daemon thread; every widget call stays there.
        threading.Thread(target=hud_mod.run_hud, args=(svc, log),
                         daemon=True).start()
        import onboarding as onboarding_mod
        if onboarding_mod.needed(cfg):
            svc.open_onboarding()
    try:
        tray.run()  # main thread owns the icon loop (stable on win32)
    finally:
        svc.shutdown()
        tray.stop()
        lock.release()
        log.info("=== service stopped ===")


if __name__ == "__main__":
    main()
