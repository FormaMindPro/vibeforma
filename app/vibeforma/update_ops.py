"""Release checks and the update flow of the service."""
from __future__ import annotations

import os
import subprocess
import sys
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
            message = self.tr("notify.update_found", ver=rel["tag"])
            excerpt = updater.changelog_excerpt(rel.get("body"))
            if excerpt:
                message += "\n" + excerpt
            self.tray.notify(message)
        if (self.cfg.get("auto_update") == "auto"
                and self.state == STATE_IDLE):
            self.apply_pending_update()

    def open_release_notes(self) -> bool:
        """Tray: what changed in the pending release, in the browser."""
        import urllib.parse
        import webbrowser

        import updater

        url = updater.release_notes_url(self.pending_update)
        if not url:
            return False
        if urllib.parse.urlsplit(url).scheme != "https":
            return False
        self.log.info("Release notes opened: %s", url)
        webbrowser.open(url)
        return True

    def update_if_newer(self, notify_latest: bool = False) -> None:
        """Backward-compatible alias: a check never installs on its own."""
        self.check_updates(notify_latest)

    def apply_pending_update(self) -> bool:
        """Install the pending release after consent (panel, tray, opt-in).

        The update runs the same plain GitHub installation command as the
        README.
        """
        rel = self.pending_update
        if not rel or self._update_busy:
            return False
        if self.state != STATE_IDLE:
            if self.tray:
                self.tray.notify(self.tr("notify.update_later", ver=rel["tag"]))
            return False
        return self._install_via_bootstrap()

    def _install_via_bootstrap(self) -> bool:
        """Run the README installation command and restart into the new build."""
        rel = self.pending_update
        if not rel or self._update_busy or self.state != STATE_IDLE:
            return False
        self._update_busy = True
        tag = rel["tag"]

        def _work() -> None:
            import updater
            self._set_state(STATE_UPDATING)
            if self.tray:
                self.tray.notify(self.tr("notify.update_applying", ver=tag))
            ok = False
            try:
                env = os.environ.copy()
                env[updater.UPDATE_TARGET_ENV] = SERVICE_DIR
                result = subprocess.run(  # noqa: S603 - fixed argv, target via env
                    updater.github_install_command(),
                    cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
                    timeout=1800,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                ok = result.returncode == 0
                tail = (result.stdout or result.stderr or "")[-800:].strip()
                if ok:
                    self.log.info("Update %s installed by the bootstrap", tag)
                else:
                    self.log.warning("Update bootstrap exit %s: %s",
                                     result.returncode, tail)
            except Exception as e:  # noqa: BLE001 - the worker must report, not crash
                self.log.warning("Update bootstrap failed: %s", e)
            self.pending_update = None
            self._set_state(STATE_IDLE)
            self._update_busy = False
            if ok:
                self._restart_after_update(tag)
            elif self.tray:
                self.tray.notify(self.tr("notify.update_install_fail", ver=tag))

        threading.Thread(target=_work, daemon=True).start()
        return True

    def dismiss_pending_update(self) -> None:
        self.pending_update = None

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
                # non-interactive contexts, cmd-start flashes a console.
                # A packed build has no interpreter: the exe restarts itself.
                if getattr(sys, "frozen", False):
                    command = [sys.executable]
                else:
                    command = [os.path.join(SERVICE_DIR, ".venv", "Scripts",
                                            "pythonw.exe"),
                               os.path.join(SERVICE_DIR, "main.py")]
                subprocess.Popen(  # noqa: S603 - fixed paths, no user input
                    command,
                    cwd=SERVICE_DIR,
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.log.info("Relaunch spawned: %s", command[0])
            except OSError:
                self.log.exception("Restart after update failed")

        # non-daemon: the interpreter waits for it, so the relaunch really
        # runs even though shutdown() tears the service down right away
        threading.Thread(target=_spawn, daemon=False).start()
        time.sleep(0.5)
        self.shutdown()

