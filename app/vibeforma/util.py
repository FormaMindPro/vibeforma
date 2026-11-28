"""Process utilities: logging, the startup beep, autostart, single instance."""
from __future__ import annotations

import contextlib
import ctypes
import io
import logging
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import wave

from config import LOG_DIR, LOG_PATH, SERVICE_DIR

PYTHONW = os.path.join(SERVICE_DIR, ".venv", "Scripts", "pythonw.exe")
MAIN_PY = os.path.join(SERVICE_DIR, "main.py")
# A packed build has no interpreter: the exe starts itself at logon.
FROZEN = bool(getattr(sys, "frozen", False))
AUTOSTART_TARGET = sys.executable if FROZEN else PYTHONW
AUTOSTART_ARGS = "" if FROZEN else f'"{MAIN_PY}"'
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


_CUE_RATE = 22050
_CUE_NOTE_MS = 80
_CUE_FADE_MS = 6
_CUE_VOLUME = 0.30
_CUE_TONES: dict[str, tuple[int, ...]] = {
    "start": (880, 1320),
    "stop": (1320, 880),
}
_CUE_LOCK = threading.Lock()


def _tone_wav(freqs: tuple[int, ...], note_ms: int) -> bytes:
    """A mono 16-bit WAV of short sine notes with an edge fade."""
    fade = max(1, int(_CUE_RATE * _CUE_FADE_MS / 1000))
    frames = bytearray()
    for freq in freqs:
        count = max(1, int(_CUE_RATE * note_ms / 1000))
        for i in range(count):
            env = min(1.0, i / fade, (count - i) / fade)
            value = int(32767 * _CUE_VOLUME * env
                        * math.sin(2 * math.pi * freq * i / _CUE_RATE))
            frames += struct.pack("<h", value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(_CUE_RATE)
        out.writeframes(bytes(frames))
    return buf.getvalue()


def _wav_path(name: str, freqs: tuple[int, ...], note_ms: int) -> str:
    """Write the tone once and return its path; a lost temp file is rebuilt."""
    folder = os.path.join(tempfile.gettempdir(), "vibeforma", "cues")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}-{note_ms}.wav")
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(_tone_wav(freqs, note_ms))
        os.replace(tmp, path)
    return path


def beep(freq: int, dur_ms: int) -> None:
    def _run() -> None:
        # The PC speaker is silent on most laptops: play a real WAV through
        # the default output device, and keep Beep as the last resort.
        try:
            import winsound
        except ImportError:
            return
        try:
            path = _wav_path(f"beep-{freq}", (freq,), dur_ms)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
        except Exception:  # noqa: BLE001 - a missing cue must not matter
            with contextlib.suppress(RuntimeError):
                winsound.Beep(freq, dur_ms)
    threading.Thread(target=_run, daemon=True).start()


def cue(kind: str) -> None:
    """Play the recording start or stop cue through the output device."""
    tones = _CUE_TONES.get(kind)
    if tones is None:
        raise ValueError(f"unknown cue: {kind}")
    try:
        import winsound
        path = _wav_path(f"cue-{kind}", tones, _CUE_NOTE_MS)
        with _CUE_LOCK:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001 - a missing cue must not matter
        beep(1200 if kind == "start" else 900, 120)


def ensure_autostart(create: bool) -> None:
    """Manage the logon shortcut from the settings checkbox."""
    log = setup_logging(False)
    try:
        if create and not os.path.exists(AUTOSTART_LNK):
            ps = (f"$ws=New-Object -ComObject WScript.Shell;"
                  f"$l=$ws.CreateShortcut('{AUTOSTART_LNK}');"
                  f"$l.TargetPath='{AUTOSTART_TARGET}';"
                  f"$l.Arguments='{AUTOSTART_ARGS}';"
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
