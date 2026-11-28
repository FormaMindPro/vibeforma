"""Settings applied to the live service and the model switch."""
from __future__ import annotations

import secrets
import threading
from typing import Any

import errors
import model_folders
from config import save

from . import STATE_ERROR, STATE_IDLE, STATE_LOADING, STATE_RECORDING
from .base import ServiceHost
from .http_api import publish_location, start_http_api
from .util import ensure_autostart


class SettingsOps(ServiceHost):
    def open_settings(self, tab: str | None = None) -> None:
        """Tray/HUD request: the HUD (main thread) opens the dialog."""
        self._ensure_hud()
        self.live["settings_tab"] = tab
        self.live["open_settings"] = True

    def open_doctor(self) -> None:
        """Tray request: the HUD opens the support report window."""
        self._ensure_hud()
        self.live["open_doctor"] = True

    def apply_settings(self, new_cfg: dict[str, Any]) -> None:
        """Save and apply to the live service; called from the HUD thread."""
        old_hotkey = self.cfg.get("hotkey")
        old_hold = bool(self.cfg.get("hold_to_talk", False))
        old_dev = self.cfg.get("input_device")
        old_api = bool(self.cfg.get("http_api"))
        old_port = int(self.cfg.get("http_port") or 8787)
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
        if ((self.cfg["hotkey"] != old_hotkey
             or bool(self.cfg.get("hold_to_talk", False)) != old_hold)
                and self.hotkey):
            import hotkey as hotkey_mod
            new_hotkey = hotkey_mod.make_hotkey(
                self.cfg["hotkey"], bool(self.cfg.get("hold_to_talk", False)),
                self.on_hotkey, self.on_hotkey_press, self.on_hotkey_release,
                self.log)
            try:
                # The new listener starts first: a failed start leaves the
                # old hotkey alive instead of leaving none.
                new_hotkey.start()
            except Exception:
                self.log.exception("Hotkey %s did not start - keeping %s",
                                   self.cfg["hotkey"], old_hotkey)
                self.cfg["hotkey"] = old_hotkey
                self.cfg["hold_to_talk"] = old_hold
                save(self.cfg)
                if self.tray:
                    self.tray.notify(self.tr("notify.hotkey_failed",
                                             combo=self.cfg["hotkey"]))
            else:
                old_listener, self.hotkey = self.hotkey, new_hotkey
                try:
                    old_listener.stop()
                except Exception as e:  # noqa: BLE001 - old listener teardown is best-effort
                    # The old listener is replaced either way; a failure to
                    # stop it is only worth a line in the log.
                    self.log.debug("Old hotkey stop: %s", e)
        if (self.cfg["input_device"] != old_dev
                and self.state == STATE_RECORDING and self.tray):
            self.tray.notify(self.tr("notify.device_next"))
        self._apply_http_api(old_api, old_port)

    def _apply_http_api(self, old_api: bool, old_port: int) -> None:
        """Start or stop the local API without a restart.

        The form's server reads the token from config.json; when the switch is
        turned on and no token exists yet, one is minted here. A port change
        restarts the listener; a failure to bind is reported, never fatal.
        """
        want = bool(self.cfg.get("http_api"))
        port = int(self.cfg.get("http_port") or 8787)
        if not want:
            if self.http_server is not None:
                self.http_server.shutdown()
                self.http_server.server_close()
                self.http_server = None
                self.log.info("HTTP API: stopped")
            publish_location(self.log, port=port, api="off")
            return
        if not str(self.cfg.get("http_token") or "").strip():
            self.cfg["http_token"] = secrets.token_urlsafe(24)
            save(self.cfg)
            self.log.info("HTTP API: token generated")
        if self.http_server is not None and old_api and port == old_port:
            publish_location(self.log,
                             port=int(self.http_server.server_address[1]),
                             api="on")
            return
        if self.http_server is not None:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
        self.http_server = start_http_api(self.cfg, self, self.log)
        actual = (int(self.http_server.server_address[1])
                  if self.http_server is not None else port)
        publish_location(self.log, port=actual,
                         api="on" if self.http_server is not None else "error")
        if self.http_server is None and self.tray:
            self.tray.notify(self.tr("notify.http_api_failed", port=port))

    def set_model(self, name: str) -> None:
        if self.state != STATE_IDLE:
            if self.tray:
                self.tray.notify(self.tr("notify.wait_cycle"))
            return

        def _load() -> None:
            self._set_state(STATE_LOADING)
            ok = self.asr.reload_model(name)
            save(self.cfg)
            self._set_state(STATE_IDLE if ok else STATE_ERROR)
            if self.tray:
                self.tray.notify(self.tr("notify.model_ok", name=name) if ok
                                 else self.tr("notify.model_failed", name=name))
        threading.Thread(target=_load, daemon=True).start()

    def switch_model(self, new_name: str, keep_old: bool,
                     progress: dict[str, Any]) -> None:
        """Settings-driven switch: download -> load -> (maybe) remove old.

        progress is a plain dict polled by the ModelSwitchDialog on the HUD
        thread (phase/name/detail/cache_note).
        """
        threading.Thread(target=self._switch_model_worker,
                         args=(new_name, keep_old, progress), daemon=True).start()

    def _switch_model_worker(self, new_name: str, keep_old: bool,
                             progress: dict[str, Any]) -> None:
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
                # reload_model had already set the new name; put the old back.
                self.cfg["model"] = old
                save(self.cfg)
                progress.update(phase="error",
                                detail=errors.text(self.cfg, "model",
                                                   RuntimeError("load failed")))
                return
            save(self.cfg)
            if not keep_old:
                from config import MODELS_DIR
                # A user model folder is never a deletion target: the switch
                # forgets the old model, it does not touch the person's files
                # (card-264). Only a downloaded copy under MODELS_DIR goes.
                target = model_folders.removal_target(old, MODELS_DIR)
                if target is None:
                    progress["local_note"] = old
                    self.log.info(
                        "Previous model %s is a local folder - kept on disk",
                        old)
                else:
                    progress.update(phase="remove", name=old)
                    if os_mod.path.isdir(target):
                        shutil.rmtree(target, ignore_errors=True)
                        self.log.info("Old model removed: %s", target)
                    else:
                        progress["cache_note"] = old
                        self.log.info(
                            "Old model %s lives in the shared HF cache — untouched",
                            old)
            progress.update(phase="done", name=new_name)
            if self.tray:
                self.tray.notify(self.tr("notify.model_ok", name=new_name))
        except Exception as e:
            self.log.exception("Model switch failed: %s", e)
            progress.update(phase="error",
                            detail=errors.text(self.cfg, "model", e))
