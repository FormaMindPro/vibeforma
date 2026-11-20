"""Process utilities: logging, the startup beep, autostart, single instance."""
from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import subprocess
import threading

from config import LOG_DIR, LOG_PATH, SERVICE_DIR

PYTHONW = os.path.join(SERVICE_DIR, ".venv", "Scripts", "pythonw.exe")
MAIN_PY = os.path.join(SERVICE_DIR, "main.py")
AUTOSTART_LNK = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    "VibeForma.lnk")


def setup_logging(console: bool) -> logging.Logger:
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


def beep(freq: int, dur_ms: int) -> None:
    def _run() -> None:
        # winsound is absent off Windows; Beep itself raises RuntimeError
        # when no device can play the tone. A missing cue must not matter.
        with contextlib.suppress(ImportError, RuntimeError):
            import winsound
            winsound.Beep(freq, dur_ms)
    threading.Thread(target=_run, daemon=True).start()


def ensure_autostart(create: bool) -> None:
    """Manage the logon shortcut from the settings checkbox."""
    log = setup_logging(False)
    try:
        if create and not os.path.exists(AUTOSTART_LNK):
            ps = (f"$ws=New-Object -ComObject WScript.Shell;"
                  f"$l=$ws.CreateShortcut('{AUTOSTART_LNK}');"
                  f"$l.TargetPath='{PYTHONW}';"
                  f"$l.Arguments='\"{MAIN_PY}\"';"
                  f"$l.WorkingDirectory='{SERVICE_DIR}';"
                  f"$l.Save()")
            # The command is built here from fixed paths, not from input.
            subprocess.run(  # noqa: S603 - a fixed PowerShell command, no user input
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",  # noqa: S607
                 "-Command", ps],
                creationflags=0x08000000, timeout=30, check=False)
            log.info("Autostart shortcut created")
        elif not create and os.path.exists(AUTOSTART_LNK):
            os.remove(AUTOSTART_LNK)
            log.info("Autostart shortcut removed")
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Autostart management: %s", e)


class SingleInstance:
    """PID-file lock with exclusive create + stale recovery.

    The second instance exits quietly; a lock left by a dead process
    (crash, power loss) is taken over.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._pid = os.getpid()
        try:
            self._take_lock()
        except FileExistsError:
            pid = self._read_pid()
            if pid and self._alive(pid):
                raise RuntimeError("another instance is already running") from None
            # stale lock from a dead process: replace it
            with contextlib.suppress(OSError):
                os.remove(path)
            self._take_lock()

    def _take_lock(self) -> None:
        fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(self._pid).encode("ascii"))
        os.close(fd)

    def _read_pid(self) -> int:
        try:
            with open(self._path) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not h:
            return False
        kernel32.CloseHandle(h)
        return True

    def release(self) -> None:
        # The lock file is best effort: a leftover is recovered next start.
        with contextlib.suppress(OSError):
            if self._read_pid() == self._pid:
                os.remove(self._path)
