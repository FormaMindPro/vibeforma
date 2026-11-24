"""Release checks and the update flow of the service."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any

from config import SERVICE_DIR, VERSION

from . import STATE_IDLE, STATE_UPDATING
from .base import ServiceHost

UPDATE_IDLE = "idle"
UPDATE_CHECKING = "checking"
UPDATE_LATEST = "latest"
UPDATE_FOUND = "found"
UPDATE_APPLYING = "applying"
UPDATE_DEFERRED = "deferred"
UPDATE_SOURCE_CHECKOUT = "source_checkout"
UPDATE_ERROR = "error"
UPDATE_INSTALL_FAILED = "install_failed"


def _remove_tree(path: str) -> None:
    """Best-effort removal of the update's temporary folder."""
    shutil.rmtree(path, ignore_errors=True)


class UpdateActions(ServiceHost):
    def check_updates(self, notify_latest: bool = False) -> None:
        """Check GitHub; a newer release only becomes pending (no silent apply).

        The result is visible in the settings header: ``latest`` or ``found``
        after a successful check, ``error`` only for a manual one. A
        background failure leaves an earlier result as it is.
        """
        import updater
        try:
            rel = updater.latest_release()
        except (OSError, ValueError) as e:
            self.log.debug("Update check failed: %s", e)
            self._note_failed_check(notify_latest)
            if notify_latest and self.tray:
                self.tray.notify(self.tr("notify.update_fail"))
            return
        if rel is None:
            self._note_failed_check(notify_latest)
            if notify_latest and self.tray:
                self.tray.notify(self.tr("notify.update_fail"))
            return
        current = updater.version_tuple(VERSION)
        if not updater.is_newer(rel["version"], current):
            # A release that is no longer newer must not stay on offer.
            self.pending_update = None
            self._update_failed = False
            self.update_check_tag = VERSION
            self.update_check_state = UPDATE_LATEST
            if notify_latest and self.tray:
                self.tray.notify(self.tr("notify.update_latest", ver=VERSION))
            return
        self.log.info("New release %s found (current %s)", rel["tag"], VERSION)
        self.pending_update = rel
        self._update_failed = False
        self.update_check_tag = rel["tag"]
        self.update_check_state = UPDATE_FOUND
        if self.tray:
            message = self.tr("notify.update_found", ver=rel["tag"])
            excerpt = updater.changelog_excerpt(rel.get("body"))
            if excerpt:
                message += "\n" + excerpt
            self.tray.notify(message)
        self._maybe_apply_pending_update()

    def _note_failed_check(self, manual: bool) -> None:
        """``error`` only after a manual check; a stale error clears on its own."""
        if manual:
            self.update_check_state = UPDATE_ERROR
        elif self.update_check_state == UPDATE_ERROR:
            self.update_check_state = UPDATE_IDLE

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

    def apply_pending_update(self, manual: bool = True) -> bool:
        """Install the pending release after consent (panel, tray, opt-in).

        The update runs the same plain GitHub installation command as the
        README; a source checkout is reported instead of overwritten, and
        an active file job or batch defers the install. A manual apply is a
        fresh attempt and clears the failed-install guard; the automatic
        retry keeps it, so a failure is never re-run in a loop.
        """
        if manual:
            self._update_failed = False
        rel = self.pending_update
        if not rel or self._update_busy:
            return False
        if self._source_checkout():
            self._note_source_checkout(rel)
            return False
        if self.state != STATE_IDLE or self._jobs_active():
            self._note_deferred(rel)
            return False
        return self._install_via_bootstrap()

    def _jobs_active(self) -> bool:
        """Whether a file job or a folder batch is still unfinished."""
        jobs = getattr(self, "file_jobs", None)
        if jobs is None:
            return False
        try:
            return bool(jobs.active_batches()) or bool(jobs.active_jobs())
        except Exception as e:  # noqa: BLE001 - update state must never break
            self.log.debug("File job state unavailable: %s", e)
            return False

    def _source_checkout(self) -> bool:
        """Whether the service runs from a git checkout, not an install."""
        import updater
        return bool(updater.is_source_checkout(SERVICE_DIR))

    def _note_source_checkout(self, rel: dict[str, Any]) -> None:
        self.log.info("Update %s skipped: the service runs from a source "
                      "checkout", rel["tag"])
        self.update_check_tag = rel["tag"]
        self.update_check_state = UPDATE_SOURCE_CHECKOUT

    def _note_deferred(self, rel: dict[str, Any]) -> None:
        self.update_check_tag = rel["tag"]
        self.update_check_state = UPDATE_DEFERRED
        if self.tray:
            self.tray.notify(self.tr("notify.update_later", ver=rel["tag"]))

    def _maybe_apply_pending_update(self) -> None:
        """Auto mode: apply once the service is idle and no job is running.

        ``apply_pending_update`` only performs quick checks and starts the
        worker thread itself, so calling it here stays non-blocking and
        claims the busy flag before another event can start a second run.
        """
        rel = self.pending_update
        if not rel or self._update_busy or self._update_failed:
            return
        if self.cfg.get("auto_update") != "auto":
            return
        if self.state != STATE_IDLE or self._jobs_active():
            return
        self.apply_pending_update(manual=False)

    def _prepare_update(self, rel: dict[str, Any]) -> tuple[str, str]:
        """Download the zip and SHA256SUMS; return verified local paths.

        The zip keeps its asset name, so the sums line matches it. A missing
        sums file, a missing line or a mismatch raises ValueError before the
        bootstrap is ever started.
        """
        import updater

        work = tempfile.mkdtemp(prefix="vibeforma-update-")
        try:
            zip_url = str(rel.get("zip") or "")
            sums_url = str(rel.get("sums") or "")
            if not zip_url:
                raise ValueError("the release has no zip asset")
            if not sums_url:
                raise ValueError("the release has no SHA256SUMS asset")
            zip_name = os.path.basename(urllib.parse.urlsplit(zip_url).path)
            if not zip_name:
                raise ValueError("the release zip URL has no file name")
            zip_path = os.path.join(work, zip_name)
            sums_path = os.path.join(work, updater.SUMS_NAME)
            updater.download_file(zip_url, zip_path)
            updater.download_file(sums_url, sums_path)
            with open(sums_path, encoding="utf-8", errors="replace") as handle:
                sums_text = handle.read()
            expected = updater.expected_sha256(sums_text, zip_name)
            if not expected:
                raise ValueError(f"SHA256SUMS has no line for {zip_name}")
            if not updater.verify_sha256(zip_path, expected):
                raise ValueError(f"SHA256 mismatch for {zip_name}")
        except Exception:
            _remove_tree(work)
            raise
        return zip_path, sums_path

    def _install_via_bootstrap(self) -> bool:
        """Run the README installation command and restart into the new build."""
        rel = self.pending_update
        if not rel or self._update_busy:
            return False
        if self._source_checkout():
            self._note_source_checkout(rel)
            return False
        if self.state != STATE_IDLE or self._jobs_active():
            self._note_deferred(rel)
            return False
        self._update_busy = True
        tag = rel["tag"]
        self.update_check_tag = tag
        self.update_check_state = UPDATE_APPLYING

        def _work() -> None:
            import updater
            self._set_state(STATE_UPDATING)
            if self.tray:
                self.tray.notify(self.tr("notify.update_applying", ver=tag))
            ok = False
            work_dir = ""
            try:
                zip_path, sums_path = self._prepare_update(rel)
                work_dir = os.path.dirname(zip_path)
                env = os.environ.copy()
                env[updater.UPDATE_TARGET_ENV] = SERVICE_DIR
                env[updater.UPDATE_ZIP_ENV] = zip_path
                env[updater.UPDATE_SUMS_ENV] = sums_path
                result = subprocess.run(  # noqa: S603 - fixed argv, paths via env
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
                self.log.warning("Update install failed: %s", e)
            finally:
                if work_dir:
                    _remove_tree(work_dir)
            if ok:
                self.pending_update = None
                self._update_failed = False
            else:
                # The guard keeps auto mode from re-running the same failure.
                self._update_failed = True
            self._set_state(STATE_IDLE)
            self._update_busy = False
            if ok:
                self.update_check_state = UPDATE_IDLE
                self._restart_after_update(tag)
            else:
                self.update_check_state = UPDATE_INSTALL_FAILED
                if self.tray:
                    self.tray.notify(
                        self.tr("notify.update_install_fail", ver=tag))

        threading.Thread(target=_work, daemon=True).start()
        return True

    def dismiss_pending_update(self) -> None:
        self.pending_update = None
        self._update_failed = False
        self.update_check_tag = ""
        self.update_check_state = UPDATE_IDLE

    def check_updates_now(self) -> None:
        """Settings button: manual check with a visible result."""
        self.update_check_state = UPDATE_CHECKING
        threading.Thread(target=self.check_updates, args=(True,),
                         daemon=True).start()

    def _restart_after_update(self, tag: str) -> None:
        """Relaunch into the new build only after the spawn really started.

        A failed spawn leaves the service running: the files are already
        updated, so the next manual start picks the new version up. The
        interpreter is looked up in the installed copy first, then in the
        parent dev clone, so a missing app/.venv never tears the service
        down.
        """
        if self.tray:
            self.tray.notify(self.tr("notify.updated_restart", ver=tag))
        self.log.info("Restarting service after update to %s", tag)
        started = threading.Event()

        def _spawn() -> None:
            time.sleep(2.0)   # let the old window/tray settle
            try:
                # detached pythonw: os.startfile proved a silent no-op in
                # non-interactive contexts, cmd-start flashes a console.
                # A packed build has no interpreter: the exe restarts itself.
                if getattr(sys, "frozen", False):
                    command = [sys.executable]
                else:
                    pythonw = self._pythonw_path()
                    if pythonw is None:
                        self.log.error("Restart skipped: no pythonw.exe in "
                                       "%s or its parent", SERVICE_DIR)
                        return
                    command = [pythonw, os.path.join(SERVICE_DIR, "main.py")]
                subprocess.Popen(  # noqa: S603 - fixed paths, no user input
                    command,
                    cwd=SERVICE_DIR,
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                started.set()
                self.log.info("Relaunch spawned: %s", command[0])
            except OSError:
                self.log.exception("Restart after update failed")

        # non-daemon: the interpreter waits for it, so the relaunch really
        # runs even though shutdown() tears the service down right away
        thread = threading.Thread(target=_spawn, daemon=False)
        thread.start()
        thread.join(timeout=15.0)
        if not started.is_set():
            self._update_failed = True
            self.update_check_state = UPDATE_INSTALL_FAILED
            self.log.error("Update %s installed, but the relaunch did not "
                           "start; the service keeps running", tag)
            if self.tray:
                self.tray.notify(
                    self.tr("notify.update_install_fail", ver=tag))
            return
        self.shutdown()

    @staticmethod
    def _pythonw_path() -> str | None:
        """pythonw.exe of the installed copy, else of the parent dev clone."""
        for base in (SERVICE_DIR, os.path.dirname(SERVICE_DIR)):
            candidate = os.path.join(base, ".venv", "Scripts", "pythonw.exe")
            if os.path.isfile(candidate):
                return candidate
        return None

