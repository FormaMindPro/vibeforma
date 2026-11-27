"""System-audio takes on disk: folder, WAV writer, TXT and SRT sidecars.

A system take is written while it runs. Capture hands 16 kHz mono frames to
one writer thread through a bounded queue, so the audio loop never waits for
the disk and memory does not grow with the take: a full queue drops a block
and counts it. The file is 16-bit PCM, 16 kHz, mono under
``<recordings>/<date>/<start time>.wav``; the full-take transcription is the
file-job queue's and lands as TXT and SRT beside the WAV with the same
basename.

The folder comes from ``system_recordings_dir``. Empty means
``Documents\\VibeForma\\Recordings`` of this machine — the Windows known
folder where it exists, never a guess — and the date subfolder is created
before capture, so an unavailable folder fails while the hotkey can still
report it.
"""
from __future__ import annotations

import contextlib
import os
import queue
import threading
import time
import wave
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
# About 16 s of 32 ms frames. A queue this size absorbs a short disk hiccup;
# a longer stall drops blocks and says so instead of growing with the take.
QUEUE_BLOCKS = 512
DATE_FORMAT = "%Y-%m-%d"
NAME_FORMAT = "%H-%M-%S"
DEFAULT_SUBFOLDER = ("VibeForma", "Recordings")
CONFIG_KEY = "system_recordings_dir"
SIDECAR_FORMATS = ("txt", "srt")


class RecordingsFolderError(RuntimeError):
    """The recordings folder cannot be used, so the take must not start."""

    def __init__(self, path: str, reason: str = "") -> None:
        self.path = str(path or "")
        self.reason = str(reason or "")
        detail = (f"{self.path} ({self.reason})" if self.reason else self.path)
        super().__init__("recordings folder unavailable: " + detail)


def _documents_folder() -> str:
    """This machine's Documents folder; the Windows known folder first."""
    if os.name == "nt":
        with contextlib.suppress(Exception):  # the fallback below always exists
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PERSONAL = 5: the shell's own Documents folder, which a
            # redirected or OneDrive profile moves without changing the name.
            if (ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0
                    and buf.value):
                return str(buf.value)
    return os.path.join(os.path.expanduser("~"), "Documents")


def default_folder() -> str:
    """``Documents\\VibeForma\\Recordings`` on this machine."""
    return os.path.join(_documents_folder(), *DEFAULT_SUBFOLDER)


def folder_for(cfg: dict[str, Any] | None) -> str:
    """The configured recordings folder, or the default when unset."""
    raw = str((cfg or {}).get(CONFIG_KEY) or "").strip().strip('"')
    if not raw:
        return default_folder()
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))


def _stamp(when: float | None) -> time.struct_time:
    return time.localtime() if when is None else time.localtime(float(when))


def ensure_folder(folder: str) -> str:
    """Create the folder or raise the named error; never a bare OSError.

    Public because the settings window checks a chosen folder with the
    writer's own rule before it saves the key: a folder the writer would
    refuse is reported in the window, not at the next take.
    """
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise RecordingsFolderError(folder, str(exc)) from exc
    if not os.path.isdir(folder):
        raise RecordingsFolderError(folder, "not a folder")
    return folder


def take_path(cfg: dict[str, Any] | None, when: float | None = None) -> str:
    """The WAV path for a take starting now: date folder plus start time.

    A collision (two takes inside one second, which the hotkey normally
    rules out) gets a numeric suffix instead of overwriting a recording.
    """
    stamp = _stamp(when)
    folder = ensure_folder(os.path.join(folder_for(cfg),
                                         time.strftime(DATE_FORMAT, stamp)))
    name = time.strftime(NAME_FORMAT, stamp)
    path = os.path.join(folder, name + ".wav")
    attempt = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{name}-{attempt}.wav")
        attempt += 1
    return path


def pcm_bytes(frame: Any) -> bytes:
    """float32 samples -> 16-bit PCM bytes, clipped to the int16 range."""
    samples = np.asarray(frame, dtype=np.float32).reshape(-1)
    if not samples.size:
        return b""
    clipped = np.clip(samples, -1.0, 1.0) * 32767.0
    return clipped.astype(np.int16).tobytes()


class WavWriter:
    """One WAV on disk: frames in through a bounded queue, a thread writes.

    The file is opened before capture starts, so an unwritable folder is
    reported while the hotkey is still in hand, not halfway through a take.
    ``push`` never blocks: a full queue drops the block and counts it.
    ``close`` flushes what is queued and closes the file; a stuck disk can
    only leave the thread to finish on its own, never hang the stop.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self.samples_written = 0
        self.dropped_blocks = 0
        self.error = ""
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._handle: Any = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lifecycle = threading.Lock()
        self._is_closed = False
        try:
            # The writer thread owns the handle until close(), so this is not
            # a with-block: closing it here would close the open take.
            handle = wave.open(self.path, "wb")  # noqa: SIM115
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(SAMPLE_RATE)
        except (OSError, ValueError, wave.Error) as exc:
            with contextlib.suppress(OSError):
                os.remove(self.path)
            raise RecordingsFolderError(os.path.dirname(self.path),
                                        str(exc)) from exc
        self._handle = handle
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vibeforma-system-wav")
        self._thread.start()

    @property
    def duration(self) -> float:
        return self.samples_written / float(SAMPLE_RATE)

    def push(self, frame: Any) -> bool:
        """Queue one frame; False when the queue is full (memory is bounded)."""
        block = pcm_bytes(frame)
        if not block:
            return True
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self.dropped_blocks += 1
            return False
        return True

    def close(self) -> bool:
        """Flush, close and join; False when a stuck disk left the thread."""
        with self._lifecycle:
            if self._is_closed:
                return True
            self._is_closed = True
        self._stopping.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                return False
        self._close_handle()
        return True

    # -- the writer thread ---------------------------------------------
    def _run(self) -> None:
        try:
            while True:
                try:
                    block = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._stopping.is_set():
                        break
                    continue
                if block is None:
                    break
                self._write(block)
            self._drain()
        finally:
            self._close_handle()

    def _drain(self) -> None:
        """Blocks queued before the stop belong to the take: write them all."""
        while True:
            try:
                block = self._queue.get_nowait()
            except queue.Empty:
                return
            if block is None:
                continue
            self._write(block)

    def _write(self, block: bytes) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.writeframes(block)
        except (OSError, ValueError, wave.Error) as exc:
            self.error = str(exc)
            return
        self.samples_written += len(block) // (SAMPLE_WIDTH * CHANNELS)

    def _close_handle(self) -> None:
        with self._lifecycle:
            handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(OSError, ValueError, wave.Error):
                handle.close()


def start_take(cfg: dict[str, Any] | None,
               when: float | None = None) -> WavWriter:
    """A writer for the take starting now; the date folder is created first."""
    return WavWriter(take_path(cfg, when=when))
