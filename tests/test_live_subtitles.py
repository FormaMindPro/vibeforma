"""Live subtitles of system audio: partials fire, the plate is bounded, no insert.

A system take is a view, never a dictation: the partial path publishes the
last one or two phrases into ``live["subtitles"]`` and leaves ``partial``
empty; stopping it starts no recognition, inserts nothing, saves no history
and shows no draft. Microphone partials keep their old field and path.
"""
import logging
import threading
import time

import pytest

np = pytest.importorskip("numpy")

from vibeforma import STATE_IDLE, STATE_RECORDING  # noqa: E402
from vibeforma.service import (  # noqa: E402
    Service,
    _subtitle_plate,
    _subtitle_rejection,
)

SAMPLE_RATE = 16000
PARTIAL_AUDIO_S = 1.0


def _vad(_window, num_samples=0):
    return np.zeros(1, dtype=np.float32)


def _no_stop(_reason):
    return None


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- the recorder fires partials in system mode ----------------------------
def _system_recorder(on_partial):
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    rec = audio.Recorder(
        {"audio_source": "system", "partial_every_s": 1.5,
         "max_record_s": 120, "silence_ms": 2000},
        logging.getLogger("test_live_subtitles"), _vad, _no_stop,
        on_partial=on_partial)
    rec._source = "system"
    rec._has_speech = True
    rec._t_last_partial = 0.0
    rec._frames = [np.zeros(audio.FRAME, dtype=np.float32)
                   for _ in range(100)]
    return rec


def test_system_partial_fires_instead_of_a_silent_return():
    audio = pytest.importorskip("audio")
    calls = []
    rec = _system_recorder(calls.append)

    rec._maybe_partial(1.5)

    assert _wait_for(lambda: bool(calls)), (
        "a system partial must not hit the old early return")
    assert 0 < len(calls[0]) <= int(audio.PARTIAL_WINDOW_S * SAMPLE_RATE)


def test_system_partial_is_throttled_to_the_system_period():
    audio = pytest.importorskip("audio")
    calls = []
    rec = _system_recorder(calls.append)

    # 2 s since the last partial would fire under the dictation period (1.5)
    # but must not fire under the system period (4 s).
    rec._t_last_partial = time.monotonic() - 2.0
    rec._maybe_partial(1.5)
    time.sleep(0.1)
    assert calls == []

    rec._t_last_partial = (time.monotonic()
                           - audio.SYSTEM_PARTIAL_EVERY_S - 0.1)
    rec._maybe_partial(1.5)
    assert _wait_for(lambda: bool(calls))


# -- the plate: last phrases, bounded, hallucination limit -----------------
def test_plate_keeps_the_last_two_phrases():
    plate = _subtitle_plate("First one. Second one. Third one.")
    assert plate == ["Second one.", "Third one."]


def test_plate_collapses_the_sliding_repeat():
    assert _subtitle_plate("Hello there. Hello there.") == ["Hello there."]


def test_plate_is_bounded():
    text = " ".join(f"w{i}" for i in range(200))
    plate = _subtitle_plate(text)
    assert len(plate) == 1
    assert len(plate[0]) <= 400


def test_two_phrases_are_capped_to_the_plate_size():
    text = "a" * 300 + ". " + "b" * 300 + "."
    plate = _subtitle_plate(text)
    total = sum(len(phrase) for phrase in plate) + max(0, len(plate) - 1)
    assert total <= 400
    assert len(plate) == 1


def test_stop_phrase_partial_is_rejected():
    assert _subtitle_rejection("Thanks for watching.") == "stop phrase"
    assert _subtitle_plate("Thanks for watching.") == []


def test_a_stop_phrase_inside_long_speech_is_kept():
    text = ("one two three four five six seven eight nine ten eleven "
            "please subscribe to my channel")
    assert _subtitle_rejection(text) is None
    assert _subtitle_plate(text)


def test_loop_partial_is_rejected():
    assert _subtitle_rejection("buy buy buy buy buy") == "repetition loop"
    assert _subtitle_plate("buy buy buy buy buy") == []


# -- the service: subtitle path publishes, the stop path does nothing ------
class RecordingLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)

    def debug(self, fmt, *args):
        self._add("debug", fmt, *args)


class FakeASR:
    def __init__(self, text):
        self.text = text

    def transcribe(self, _audio, verbose=False, partial=False):
        return self.text


class FakeRecorder:
    def __init__(self, source):
        self._source = source
        self.stopped = False
        self.path = ""

    def stop(self):
        self.stopped = True
        return np.zeros(SAMPLE_RATE, dtype=np.float32)

    def recording_path(self):
        return self.path


class FakeJobs:
    """The file queue as the stop path sees it: submission and nothing else."""

    def __init__(self):
        self.submitted = []

    def submit(self, path, **kwargs):
        self.submitted.append((path, kwargs))
        return None


def _service(text="", source="system"):
    svc = Service.__new__(Service)
    svc.cfg = {"audio_source": source, "locale": "en", "app_profiles": []}
    svc.log = RecordingLog()
    svc.state = STATE_RECORDING
    svc.asr = FakeASR(text)
    svc.live = {"state": STATE_RECORDING, "partial": "", "subtitles": [],
                "level": 0.0}
    svc._partial_busy = False
    svc.recorder = FakeRecorder(source)
    svc._record_started = time.time()
    svc._record_seconds = 0.0
    svc._panel_mode = False
    svc._context_hotwords = ""
    svc._app_profile_ready = False
    svc._running = True
    return svc


def test_system_partial_publishes_subtitles_not_a_partial():
    svc = _service("First one. Second one.")

    svc._on_partial(np.zeros(int(PARTIAL_AUDIO_S * SAMPLE_RATE),
                             dtype=np.float32))

    assert _wait_for(lambda: not svc._partial_busy)
    assert svc.live["subtitles"] == ["First one.", "Second one."]
    assert svc.live["partial"] == ""


def test_mic_partial_keeps_the_partial_field():
    svc = _service("hello", source="microphone")
    svc._chunk_lock = threading.Lock()
    svc._chunk_texts = ["final"]

    svc._on_partial(np.zeros(int(PARTIAL_AUDIO_S * SAMPLE_RATE),
                             dtype=np.float32))

    assert _wait_for(lambda: not svc._partial_busy)
    assert svc.live["partial"] == "final hello"
    assert svc.live["subtitles"] == []


def test_system_stop_queues_the_recording_and_inserts_nothing(tmp_path):
    svc = _service("", source="system")
    svc.live["subtitles"] = ["Last phrase."]
    states = []
    svc._set_state = states.append
    called = []
    svc._transcribe_worker = lambda *args, **kw: called.append("recognize")
    svc._finish_chunks = lambda: called.append("chunks")
    wav = tmp_path / "take.wav"
    wav.write_bytes(b"RIFF")
    jobs = FakeJobs()
    svc.file_jobs = jobs
    rec = svc.recorder
    rec.path = str(wav)

    svc.stop_recording("panel")

    assert rec.stopped
    assert svc.recorder is None
    assert states == [STATE_IDLE]
    # The dictation pass and the chunk path stay out: the take is a file job.
    assert called == []
    assert jobs.submitted == [(str(wav), {"formats": ["txt", "srt"],
                                          "trusted": True})]
    assert svc.live["partial"] == ""
    assert svc.live["level"] == 0.0
    assert svc.live["subtitles"] == ["Last phrase."]
    assert "draft" not in svc.live


def test_mic_stop_still_starts_recognition():
    svc = _service("", source="microphone")
    states = []
    svc._set_state = states.append
    svc._finish_chunks = lambda: None
    called = []
    svc._transcribe_worker = lambda *args, **kw: called.append(args)

    svc.stop_recording("panel")

    assert _wait_for(lambda: bool(called))
    assert states == ["processing"]
