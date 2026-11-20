"""Release checks and the update flow of the service."""
from __future__ import annotations

import os
import subprocess
import threading
import time

from config import SERVICE_DIR, VERSION

from . import STATE_IDLE, STATE_UPDATING
from .base import ServiceHost


class UpdateActions(ServiceHost):
    def check_updates(self, notify_latest: bool = False) -> None:
        """Check GitHub; a newer release only becomes pending (no silent apply)."""
        import updater
        try:
            rel = updater.latest_release()
        except (OSError, ValueError) as e:
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

    def update_if_newer(self, notify_latest: bool = False) -> None:
        """Backward-compatible alias: a check never installs on its own."""
        self.check_updates(notify_latest)

    def apply_pending_update(self) -> bool:
        """Install the pending release after consent (tray) or explicit opt-in."""
        rel = self.pending_update
        if not rel or self._update_busy:
            return False
        if self.state != STATE_IDLE:
            if self.tray:
                self.tray.notify(self.tr("notify.update_later", ver=rel["tag"]))
            return False
        self._update_busy = True

        def _work() -> None:
            import updater
            self._set_state(STATE_UPDATING)
            if self.tray:
                self.tray.notify(self.tr("notify.update_applying",
                                         ver=rel["tag"]))
            ok, reason = updater.apply(rel, SERVICE_DIR, self.log)
            self._update_busy = False
            self.pending_update = None
            self._set_state(STATE_IDLE)
            if ok:
                self.restart_pending = rel["tag"]
                if self.cfg.get("auto_update") == "auto":
                    self._restart_after_update(rel["tag"])
                elif self.tray:
                    self.tray.notify(self.tr("notify.update_installed",
                                             ver=rel["tag"]))
            elif self.tray:
                self.log.warning("Update was not applied: %s", reason)
                self.tray.notify(self.tr("notify.update_refused"))

        threading.Thread(target=_work, daemon=True).start()
        return True

    def dismiss_pending_update(self) -> None:
        self.pending_update = None

    def restart_for_update(self) -> None:
        tag = self.restart_pending
        self.restart_pending = None
        if tag:
            self._restart_after_update(tag)

    def check_updates_now(self) -> None:
        """Settings button: manual check with a visible result."""
        threading.Thread(target=self.check_updates, args=(True,),
                         daemon=True).start()

    def _restart_after_update(self, tag: str) -> None:
        if self.tray:
            self.tray.notify(self.tr("notify.updated_restart", ver=tag))
        self.log.info("Restarting service after update to %s", tag)

        def _spawn() -> None:
            time.sleep(2.0)   # let the old window/tray settle
            try:
                # detached pythonw: os.startfile proved a silent no-op in
                # non-interactive contexts, cmd-start flashes a console
                pythonw = os.path.join(SERVICE_DIR, ".venv", "Scripts",
                                       "pythonw.exe")
                subprocess.Popen(  # noqa: S603 - a fixed interpreter path, no user input
                    [pythonw, os.path.join(SERVICE_DIR, "main.py")],
                    cwd=SERVICE_DIR,
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.log.info("Relaunch spawned: %s", pythonw)
            except OSError:
                self.log.exception("Restart after update failed")

        # non-daemon: the interpreter waits for it, so the relaunch really
        # runs even though shutdown() tears the service down right away
        threading.Thread(target=_spawn, daemon=False).start()
        time.sleep(0.5)
        self.shutdown()

