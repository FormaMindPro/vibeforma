"""System audio on disk: WAV, folder, TXT/SRT beside it, no dictation.

A system take is a recording, not a dictation. Frames go through a bounded
writer queue to a 16-bit PCM 16 kHz mono WAV under the date subfolder of the
recordings folder; stopping the take queues it through the file entrance,
which writes TXT and SRT beside it. Nothing is inserted and the dictation
history is never touched. A folder that cannot be used refuses the start
with a localized error.

Nothing here needs an audio device at import time: the writer and the service
paths use fakes and ``tmp_path``; the few tests that exercise the real
``Recorder`` skip where ``sounddevice`` is absent (the Linux release run).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import system_recording  # noqa: E402
import transcript  # noqa: E402
from vibeforma import STATE_IDLE, STATE_RECORDING  # noqa: E402
from vibeforma import service as service_mod  # noqa: E402
from vibeforma.service import Service  # noqa: E402

SAMPLE_RATE = system_recording.SAMPLE_RATE
FRAME = 512


# -- helpers -------------------------------------------------------------
def _tone(seconds: float, freq: float = 440.0) -> np.ndarray:
    count = int(SAMPLE_RATE * seconds)
    t = np.arange(count, dtype=np.float32) / SAMPLE_RATE
    return (0.2 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _blocks(audio: np.ndarray, size: int = FRAME) -> list[np.ndarray]:
    return [audio[i:i + size] for i in range(0, len(audio), size)]


def _record(cfg: dict, seconds: float = 0.5) -> str:
    """One finished take through the real writer; returns the WAV path."""
    writer = system_recording.start_take(cfg)
    for block in _blocks(_tone(seconds)):
        writer.push(block)
    assert writer.close() is True
    return writer.path


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _add(self, level: str, fmt: str, *args: object) -> None:
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt: str, *args: object) -> None:
        self._add("info", fmt, *args)

    def warning(self, fmt: str, *args: object) -> None:
        self._add("warning", fmt, *args)

    def exception(self, fmt: str, *args: object) -> None:
        self._add("error", fmt, *args)

    def debug(self, fmt: str, *args: object) -> None:
        pass


class _Tray:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str, title: str = "VibeForma") -> None:
        self.messages.append(message)


class _FakeASR:
    """A file engine: one segment, no decoding, the configured model name."""

    model_name = "fake"

    def transcribe_file(self, _path: str, **kwargs: object):
        on_duration = kwargs.get("on_duration")
        if callable(on_duration):
            on_duration(1.0)
        yield transcript.Segment(0.0, 1.0, " hello")


class _FakeRecorder:
    def __init__(self, path: str = "", source: str = "system") -> None:
        self._source = source
        self.path = path
        self.stopped = False

    def stop(self) -> np.ndarray:
        self.stopped = True
        return np.zeros(SAMPLE_RATE // 10, dtype=np.float32)

    def recording_path(self) -> str:
        return self.path


class _FailingRecorder:
    _source = "system"

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[str] = []

    def start(self, _device_index=None) -> None:
        self.calls.append("start")
        raise system_recording.RecordingsFolderError(self.path, "denied")

    def recording_path(self) -> str:
        return ""


def _service(cfg: dict) -> Service:
    svc = Service.__new__(Service)
    svc.cfg = cfg
    svc.log = _Log()
    svc.state = STATE_RECORDING
    svc.asr = None
    svc.tray = None
    svc.recorder = None
    svc.file_jobs = None
    svc.live = {"state": STATE_RECORDING, "partial": "", "subtitles": [],
                "level": 0.0, "recording_path": "", "draft": "",
                "last": "", "last_ts": 0.0}
    svc._partial_busy = False
    svc._record_started = time.time()
    svc._record_seconds = 0.0
    svc._panel_mode = False
    svc._context_hotwords = ""
    svc._app_profile_ready = False
    svc._test_mode = False
    svc._running = True
    return svc


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- folder and path -----------------------------------------------------
def test_the_default_folder_is_documents_vibeforma_recordings():
    path = system_recording.default_folder()
    assert path.endswith(os.path.join("Documents", "VibeForma", "Recordings"))
    assert system_recording.folder_for({}) == path
    assert system_recording.folder_for(
        {"system_recordings_dir": "  "}) == path


def test_take_path_uses_the_date_subfolder_and_the_start_time(tmp_path):
    cfg = {"system_recordings_dir": str(tmp_path)}
    when = time.mktime((2026, 11, 26, 14, 30, 5, 0, 0, -1))

    path = system_recording.take_path(cfg, when=when)

    assert os.path.dirname(path) == str(tmp_path / "2026-11-26")
    assert os.path.basename(path) == "14-30-05.wav"
    assert os.path.isdir(os.path.dirname(path))


def test_a_second_take_in_the_same_second_keeps_the_first_file(tmp_path):
    cfg = {"system_recordings_dir": str(tmp_path)}
    when = time.mktime((2026, 11, 26, 14, 30, 5, 0, 0, -1))
    first = system_recording.take_path(cfg, when=when)
    Path(first).write_bytes(b"RIFF")

    second = system_recording.take_path(cfg, when=when)

    assert os.path.basename(second) == "14-30-05-2.wav"


def test_an_unusable_folder_raises_the_named_error(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder", encoding="utf-8")

    with pytest.raises(system_recording.RecordingsFolderError) as info:
        system_recording.start_take({"system_recordings_dir": str(blocker)})

    assert str(blocker) in info.value.path


# -- the WAV -------------------------------------------------------------
def test_the_wav_is_16_bit_16_khz_mono_and_holds_the_whole_take(tmp_path):
    writer = system_recording.start_take(
        {"system_recordings_dir": str(tmp_path)})
    tone = _tone(2.0)
    for block in _blocks(tone):
        writer.push(block)
    assert writer.close() is True

    assert writer.dropped_blocks == 0
    assert writer.duration == pytest.approx(2.0, abs=0.001)
    with wave.open(writer.path, "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnframes() == len(tone)
        assert (handle.getnframes() / handle.getframerate()
                == pytest.approx(2.0, abs=0.001))
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    assert int(np.abs(samples).max()) > 1000


def test_the_writer_queue_is_bounded_and_drops_instead_of_growing(tmp_path):
    writer = system_recording.start_take(
        {"system_recordings_dir": str(tmp_path)})
    assert writer._queue.maxsize == system_recording.QUEUE_BLOCKS
    delay = [0.01]

    def slow_write(_block: bytes) -> None:
        time.sleep(delay[0])

    writer._write = slow_write  # type: ignore[method-assign]
    block = np.zeros(FRAME, dtype=np.float32)
    started = time.monotonic()
    for _ in range(system_recording.QUEUE_BLOCKS + 200):
        writer.push(block)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert writer._queue.qsize() <= system_recording.QUEUE_BLOCKS
    assert writer.dropped_blocks > 0
    delay[0] = 0.0
    assert writer.close() is True


# -- the real capture path writes the file -------------------------------
def test_the_capture_path_writes_every_frame_and_stop_closes_the_file(tmp_path):
    pytest.importorskip("sounddevice")
    audio = pytest.importorskip("audio")
    cfg = {"audio_source": "system", "max_record_s": 120, "silence_ms": 2000,
           "system_recordings_dir": str(tmp_path)}
    rec = audio.Recorder(cfg, logging.getLogger("test_system_recording"),
                         lambda _w, num_samples=0: np.zeros(1, np.float32),
                         lambda _reason: None)
    writer = system_recording.start_take(cfg)
    rec._writer = writer
    rec._recording_path = writer.path
    rec._source = "system"
    tone = _tone(1.0)

    for block in _blocks(tone):
        rec._push(block)
    path = rec.recording_path()
    rec.stop()

    with wave.open(path, "rb") as handle:
        assert handle.getnframes() == len(tone)
        assert handle.getframerate() == SAMPLE_RATE


def test_a_real_system_take_writes_the_wav_end_to_end(tmp_path, monkeypatch):
    pytest.importorskip("sounddevice")
    audio = pytest.importorskip("audio")
    system_audio = pytest.importorskip("system_audio")

    class FakeCapture:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.closed = True
            return False

        def record(self, numframes):
            time.sleep(0.01)
            if self.closed:
                raise RuntimeError("the loopback recorder is closed")
            return np.full((numframes, 2), 0.2, dtype=np.float32)

    class FakeMic:
        def recorder(self, samplerate, channels):
            assert samplerate == 48000
            assert channels == 2
            return FakeCapture()

    monkeypatch.setattr(system_audio, "default_loopback",
                        lambda: (FakeMic(), "Fake output"))
    cfg = {"audio_source": "system", "max_record_s": 120, "silence_ms": 2000,
           "system_recordings_dir": str(tmp_path)}
    rec = audio.Recorder(cfg, logging.getLogger("test_system_recording"),
                         lambda _w, num_samples=0: np.zeros(1, np.float32),
                         lambda _reason: None)

    rec.start()
    time.sleep(0.2)
    path = rec.recording_path()
    rec.stop()

    assert path and os.path.isfile(path)
    with wave.open(path, "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth(),
                handle.getframerate()) == (1, 2, SAMPLE_RATE)
        assert handle.getnframes() > 0
        raw = handle.readframes(handle.getnframes())
    assert int(np.abs(np.frombuffer(raw, dtype=np.int16)).max()) > 0


def test_the_live_buffer_stays_capped_at_30_seconds(tmp_path):
    pytest.importorskip("sounddevice")
    audio = pytest.importorskip("audio")
    cfg = {"audio_source": "system", "max_record_s": 120, "silence_ms": 2000,
           "system_recordings_dir": str(tmp_path)}
    rec = audio.Recorder(cfg, logging.getLogger("test_system_recording"),
                         lambda _w, num_samples=0: np.zeros(1, np.float32),
                         lambda _reason: None)
    rec._source = "system"
    limit = int(audio.SYSTEM_BUFFER_S * SAMPLE_RATE) // audio.FRAME

    for _ in range(limit + 200):
        rec._ingest(np.zeros(audio.FRAME, dtype=np.float32))

    assert len(rec._frames) == limit
    assert audio.SYSTEM_BUFFER_S == 30.0


def test_the_real_capture_path_refuses_an_unusable_folder(tmp_path):
    pytest.importorskip("sounddevice")
    audio = pytest.importorskip("audio")
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    cfg = {"audio_source": "system", "max_record_s": 120, "silence_ms": 2000,
           "system_recordings_dir": str(blocker)}
    rec = audio.Recorder(cfg, logging.getLogger("test_system_recording"),
                         lambda _w, num_samples=0: np.zeros(1, np.float32),
                         lambda _reason: None)

    with pytest.raises(system_recording.RecordingsFolderError):
        rec.start()

    assert rec.recording_path() == ""


# -- the service: start refuses, stop queues, nothing is dictated ---------
@pytest.mark.parametrize(("locale", "needle"),
                         [("en", "recordings folder is unavailable"),
                          ("ru", "\u043f\u0430\u043f\u043a\u0430 \u0434\u043b\u044f "
                                 "\u0437\u0430\u043f\u0438\u0441\u0435\u0439 "
                                 "\u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430")])
def test_an_unusable_folder_shows_a_localized_error_and_no_take(
        tmp_path, monkeypatch, locale, needle):
    svc = _service({"audio_source": "system", "locale": locale,
                    "input_device": ""})
    tray = _Tray()
    svc.tray = tray
    states: list[str] = []

    def _set_state(state: str) -> None:
        svc.state = state
        states.append(state)

    failing = _FailingRecorder(str(tmp_path / "blocked"))
    monkeypatch.setattr(service_mod, "find_input_device", lambda _value: None)
    svc._set_state = _set_state  # type: ignore[method-assign]
    svc._forget_draft = lambda: None  # type: ignore[method-assign]
    svc._reset_chunks = lambda: None  # type: ignore[method-assign]
    svc._make_recorder = lambda: failing  # type: ignore[method-assign]
    svc._panel_mode = True
    svc._cancel_evt = threading.Event()

    svc.start_recording()

    assert failing.calls == ["start"]
    assert states == [STATE_IDLE]
    assert svc.state == STATE_IDLE
    assert svc.live["recording_path"] == ""
    assert needle in svc.live["last"]
    assert str(tmp_path / "blocked") in svc.live["last"]
    assert tray.messages and needle in tray.messages[-1]


def test_stop_queues_the_take_and_writes_txt_and_srt_beside_the_wav(tmp_path):
    file_jobs = pytest.importorskip("file_jobs")
    wav = _record({"system_recordings_dir": str(tmp_path)})
    svc = _service({"audio_source": "system", "locale": "en"})
    svc.recorder = _FakeRecorder(wav)
    remembered: list[object] = []
    dictated: list[object] = []
    states: list[str] = []
    events: list[object] = []

    def _set_state(state: str) -> None:
        svc.state = state
        states.append(state)

    svc._set_state = _set_state  # type: ignore[method-assign]
    svc._remember = lambda *args, **kwargs: remembered.append(args)
    svc._transcribe_worker = lambda *args, **kwargs: dictated.append(args)
    jobs = file_jobs.FileJobs({"language": "en", "model": "fake",
                               "file_roots": [], "job_keep_s": 60,
                               "file_word_timestamps": False},
                              _FakeASR(), svc.log, on_event=events.append)
    svc.file_jobs = jobs
    try:
        svc.stop_recording("manual")

        assert svc.recorder is None
        assert states == [STATE_IDLE]
        assert svc.live["partial"] == ""
        assert svc.live["recording_path"] == ""
        assert dictated == []
        assert remembered == []
        assert _wait(lambda: any(
            getattr(job, "state", "") in file_jobs.TERMINAL
            for job in events))
        job = next(job for job in events
                   if job.source == os.path.realpath(wav))
        assert job.state == "done"
        stem = os.path.splitext(wav)[0]
        assert Path(stem + ".txt").read_text(encoding="utf-8").strip() == "hello"
        srt = Path(stem + ".srt").read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:01,000" in srt
        assert "hello" in srt
        assert {os.path.realpath(path) for path in job.outputs} == {
            os.path.realpath(stem + ".txt"),
            os.path.realpath(stem + ".srt")}
    finally:
        jobs.shutdown()


def test_stop_of_an_empty_take_reports_it_and_queues_nothing(tmp_path):
    svc = _service({"audio_source": "system", "locale": "en"})
    svc.recorder = _FakeRecorder(str(tmp_path / "missing.wav"))
    submitted: list[str] = []
    svc._set_state = lambda state: None  # type: ignore[method-assign]
    svc.file_jobs = type("Jobs", (), {
        "submit": lambda self, path, **kwargs: submitted.append(path)})()
    tray = _Tray()
    svc.tray = tray

    svc.stop_recording("manual")

    assert submitted == []
    assert "Nothing was recorded" in svc.live["last"]
    assert tray.messages and "Nothing was recorded" in tray.messages[-1]


def test_shutdown_closes_a_take_in_flight(tmp_path):
    wav = _record({"system_recordings_dir": str(tmp_path)})
    svc = Service.__new__(Service)
    svc._running = True
    svc.live = {"exit": False}
    svc.pipe = None
    svc.file_jobs = None
    svc.hotkey = None
    svc.cancel_watcher = None
    svc.tray = None
    recorder = _FakeRecorder(wav)
    svc.recorder = recorder

    svc.shutdown()

    assert recorder.stopped is True
    assert svc.recorder is None
