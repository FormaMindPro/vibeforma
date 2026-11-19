"""VibeForma — local voice dictation service for Windows.

Hotkey -> mic recording -> local recognition (faster-whisper, ~99 languages)
-> text inserted into the focused field (clipboard + Shift+Insert).
Fully local: the network is used only for the first model download.
"""
import sys
import os
import subprocess
import threading
import time

from config import (CONFIG_PATH, LOCK_PATH, LOG_DIR, LOG_PATH,
                    SERVICE_DIR, TEST_WAV, VERSION, find_input_device,
                    input_device_names, load, save)

STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_PAUSED = "paused"
STATE_ERROR = "error"


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
        # shared live view for the HUD (written by worker threads, read by GUI)
        self.live = {
            "state": STATE_LOADING,
            "level": 0.0,
            "vad": 0.0,
            "partial": "",
            "last": "",
            "device": "",
            "exit": False,
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
        self.log.info("Paste target window: %s (hwnd=%s)",
                      insert_mod.foreground_window(), self._target_hwnd)
        self.recorder = self._make_recorder()
        try:
            self.recorder.start(dev_idx)
        except Exception as e:
            self.log.exception("Microphone unavailable: %s", e)
            self._set_state(STATE_IDLE)
            if self.tray:
                self.tray.notify(self.tr("notify.mic_unavailable", err=e))
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
                self.log.info("No speech captured — nothing inserted")
                self.live["last"] = self.tr("notify.no_speech_last")
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.no_speech"))
                return
            t0 = time.time()
            text = self.asr.transcribe(buf)
            dt = time.time() - t0
            self.log.info("Latency to ready text: %.2f s", dt)
            if not text:
                self.log.info("Empty transcription — nothing inserted")
                self.live["last"] = self.tr("notify.not_recognized")
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.not_recognized_short"))
                return
            import insert as insert_mod
            insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=getattr(self, "_target_hwnd", None),
                paste_method=self.cfg.get("paste_method", "shift_insert"))
            self.live["last"] = self.tr(
                "notify.inserted",
                text=text[:90] + ("…" if len(text) > 90 else ""))
            self.live["partial"] = ""
            self.live["level"] = 0.0
            if self.tray:
                self.tray.notify(self.tr(
                    "notify.inserted",
                    text=text[:80] + ("…" if len(text) > 80 else "")))
        except Exception as e:
            self.log.exception("Recognition error: %s", e)
            beep(400, 200)
            self.live["last"] = self.tr("notify.error_last", err=e)
            if self.tray:
                self.tray.notify(self.tr("notify.error", err=e))
        finally:
            if self._running:
                self._set_state(STATE_IDLE)

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

    # -- settings ----------------------------------------------------------
    def open_settings(self):
        """Tray/HUD request: the HUD (main thread) opens the dialog."""
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
        self.live["last"] = self.tr("notify.saved")
        ensure_autostart(bool(self.cfg.get("autostart", True)))
        if self.cfg["hotkey"] != old_hotkey and self.hotkey:
            try:
                self.hotkey.stop()
            except Exception:
                pass
            import hotkey as hotkey_mod
            self.hotkey = hotkey_mod.Hotkey(self.cfg["hotkey"],
                                            self.on_hotkey, self.log)
            self.hotkey.start()
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
                progress.update(phase="error", detail=str(e))
                return
            progress.update(phase="load", name=new_name)
            ok = self.asr.reload_model(new_name)
            if not ok:
                self.cfg["model"] = old   # reload_model had already set the new name
                save(self.cfg)
                progress.update(phase="error", detail="load failed")
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
            progress.update(phase="error", detail=str(e))

    # -- updates -----------------------------------------------------------
    def update_if_newer(self, notify_latest: bool = False):
        """Check GitHub; if a newer release exists — download, install, restart."""
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
        if self.state != STATE_IDLE:
            if self.tray:
                self.tray.notify(self.tr("notify.update_later", ver=rel["tag"]))
            return
        self.log.info("New release %s found (current %s)", rel["tag"], VERSION)
        if self.tray:
            self.tray.notify(self.tr("notify.update_found", ver=rel["tag"]))
        if updater.apply(rel, SERVICE_DIR, self.log):
            self._restart_after_update(rel["tag"])
        elif self.tray:
            self.tray.notify(self.tr("notify.update_fail"))

    def check_updates_now(self):
        """Settings button: manual check with a visible result."""
        threading.Thread(target=self.update_if_newer, args=(True,),
                         daemon=True).start()

    def _restart_after_update(self, tag):
        if self.tray:
            self.tray.notify(self.tr("notify.updated_restart", ver=tag))

        def _spawn():
            time.sleep(2.0)
            try:
                os.startfile(os.path.join(SERVICE_DIR, "run.cmd"))
            except Exception:
                self.log.exception("Restart after update failed")

        threading.Thread(target=_spawn, daemon=True).start()
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
                    self.tray.notify(self.tr("notify.model_error", err=e))
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
def start_http_api(cfg, svc, log):
    """Optional local API: POST http://127.0.0.1:8787/transcribe (WAV body)."""
    import json as json_mod
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/transcribe" or svc.state != STATE_IDLE:
                self.send_error(409 if svc.state != STATE_IDLE else 404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                tmp = os.path.join(LOG_DIR, "_http_in.wav")
                with open(tmp, "wb") as f:
                    f.write(data)
                import audio as audio_mod
                audio = audio_mod.load_wav_16k(tmp)
                text = svc.asr.transcribe(audio)
                body = json_mod.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                log.exception("HTTP API error: %s", e)
                self.send_error(500)

        def log_message(self, fmt, *args):
            log.debug("HTTP: " + fmt % args)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(cfg["http_port"])), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("HTTP API: http://127.0.0.1:%d/transcribe", cfg["http_port"])
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
                    svc.update_if_newer()
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
def main():
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
    try:
        tray.run()  # main thread owns the icon loop (stable on win32)
    finally:
        svc.shutdown()
        tray.stop()
        lock.release()
        log.info("=== service stopped ===")


if __name__ == "__main__":
    main()
