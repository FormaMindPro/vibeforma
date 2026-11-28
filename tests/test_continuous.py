"""Continuous dictation: long speech is chunked at pauses, one insertion."""
import logging
import sys
import threading
import time
import types

import pytest

try:
    import sounddevice  # noqa: F401
except Exception:  # noqa: BLE001 - init fails on a host with no audio server
    pytest.skip("sounddevice cannot initialize here", allow_module_level=True)

import audio
import numpy as np
from vibeforma import STATE_IDLE, STATE_PROCESSING, STATE_RECORDING
from vibeforma.service import Service, join_chunks

LOG = logging.getLogger("test_continuous")
SR = audio.SAMPLE_RATE


class _Vad:
    """A VAD whose verdict the test sets; one frame, one probability."""

    def __init__(self, prob=0.0):
        self.prob = prob

    def __call__(self, _window, num_samples=0):
        return np.array([self.prob], dtype=np.float32)


def _cfg(**over):
    cfg = {
        "continuous": True,
        "max_record_s": 120,
        "silence_ms": 2000,
        "auto_stop": True,
        "partial_every_s": 1.5,
        "audio_source": "microphone",
    }
    cfg.update(over)
    return cfg


def _recorder(cfg, chunks, vad=None):
    def on_chunk(chunk):
        chunks.append(chunk)
        return True

    rec = audio.Recorder(cfg, LOG, vad or _Vad(), lambda reason: None,
                         on_chunk=on_chunk)
    rec._reset_take()   # what start() does before a real device is opened
    rec._t_start = time.monotonic()
    return rec


def _feed(rec, seconds, prob):
    """Feed whole frames at the given VAD verdict, as the monitor would."""
    count = int(round(seconds * SR / audio.FRAME))
    rec.vad.prob = prob
    data = (np.full(audio.FRAME, 0.1, dtype=np.float32) if prob
            else np.zeros(audio.FRAME, dtype=np.float32))
    for _ in range(count):
        rec._ingest(data)


# -- the mode switch -------------------------------------------------------

def test_continuous_take_is_not_cut_by_max_record_s():
    chunks = []
    rec = _recorder(_cfg(continuous=True, max_record_s=1), chunks)
    rec._t_start = time.monotonic() - 2.0

    rec._check_timeouts()

    assert not rec._stop_evt.is_set()

    legacy = _recorder(_cfg(continuous=False, max_record_s=1), chunks)
    legacy._t_start = time.monotonic() - 2.0

    legacy._check_timeouts()

    assert legacy._stop_evt.is_set()


def test_the_service_wires_the_continuous_recorder(monkeypatch):
    captured = {}

    class _Rec:
        def __init__(self, cfg, log, vad, on_auto_stop, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(audio, "Recorder", _Rec)
    svc = _service()

    svc._make_recorder()

    assert captured["on_chunk"] == svc._on_audio_chunk


# -- chunk cuts ------------------------------------------------------------

def test_a_pause_closes_a_chunk_and_the_take_keeps_going():
    chunks = []
    rec = _recorder(_cfg(), chunks, _Vad())

    _feed(rec, 2.0, 1.0)
    assert chunks == []

    _feed(rec, audio.CONTINUOUS_PAUSE_MS / 1000.0 + 0.1, 0.0)

    assert len(chunks) == 1
    assert abs(len(chunks[0]) - 2.0 * SR) < 2 * audio.FRAME
    assert not rec._stop_evt.is_set()
    # Only the trailing silence is left: the spoken part is out of memory.
    assert rec._frames
    assert rec._chunk_samples == len(rec._frames) * audio.FRAME
    assert rec._chunk_samples < audio.SAMPLE_RATE


def test_a_run_on_speaker_is_cut_at_the_chunk_cap_with_an_overlap():
    chunks = []
    rec = _recorder(_cfg(), chunks, _Vad())
    rec.vad.prob = 1.0
    frame = np.full(audio.FRAME, 0.1, dtype=np.float32)

    while not chunks:
        rec._ingest(frame)

    assert len(chunks) == 1
    assert len(chunks[0]) >= int(audio.CONTINUOUS_CHUNK_S * SR)
    # The cut keeps the last frames for the next chunk: the word that
    # straddles the cap is complete in the next recognition.
    overlap = int(audio.CONTINUOUS_OVERLAP_S * SR)
    assert 0 < rec._chunk_samples <= overlap + audio.FRAME


def test_a_three_minute_take_stays_bounded_and_keeps_going():
    chunks = []
    rec = _recorder(_cfg(), chunks, _Vad())
    rec._t_start = time.monotonic() - 121.0
    cap = int(audio.CONTINUOUS_CHUNK_S * SR) + audio.FRAME

    _feed(rec, 180.0, 1.0)

    assert not rec._stop_evt.is_set()
    assert len(chunks) >= 5
    assert sum(len(chunk) for chunk in chunks) >= 170 * SR
    assert rec._chunk_samples <= cap


def test_silence_before_speech_never_opens_a_chunk():
    chunks = []
    rec = _recorder(_cfg(), chunks, _Vad())

    _feed(rec, 2.0, 0.0)

    assert chunks == []
    assert not rec._stop_evt.is_set()


# -- seam stitching --------------------------------------------------------

def test_repeated_seam_words_are_dropped():
    assert (join_chunks(["hello world how", "how are you"])
            == "hello world how are you")
    assert (join_chunks(["one two three", "two three four"])
            == "one two three four")
    privet = "\u041f\u0440\u0438\u0432\u0435\u0442, \u043c\u0438\u0440"
    mir = "\u043c\u0438\u0440 \u043a\u0430\u043a \u0434\u0435\u043b\u0430"
    assert (join_chunks([privet, mir])
            == privet + " \u043a\u0430\u043a \u0434\u0435\u043b\u0430")


def test_a_sentence_end_survives_the_seam():
    assert (join_chunks(["First sentence.", "Second sentence."])
            == "First sentence. Second sentence.")
    assert join_chunks(["Hello, world", "world."]) == "Hello, world."


def test_a_word_split_by_the_hard_cut_is_completed():
    assert (join_chunks(["we were recog", "recognition is hard"])
            == "we were recognition is hard")


def test_one_piece_and_no_pieces():
    assert join_chunks(["hello there"]) == "hello there"
    assert join_chunks([]) == ""
    assert join_chunks(["", "   "]) == ""


# -- the service: finals first, one insertion ------------------------------

class _Log:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class _Asr:
    """Returns the queued texts in call order, recording each call."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []
        self.last_segments = []

    def transcribe(self, samples, verbose=True, *, partial=False):
        self.calls.append((len(samples), verbose, partial))
        return self.texts.pop(0) if self.texts else ""


class _TailRecorder:
    def had_speech(self):
        return True


def _service():
    cfg = {
        "locale": "en",
        "dictionary": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
        "continuous": True,
    }
    svc = Service(cfg, _Log())
    svc.stats = None
    return svc


def _fake_insert(monkeypatch):
    calls = []
    fake = types.ModuleType("insert")
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.get_foreground_hwnd = lambda: 0
    fake.insert_text = lambda text, **kwargs: calls.append(text) or "inserted"
    monkeypatch.setitem(sys.modules, "insert", fake)
    return calls


def test_chunks_are_final_passes_and_the_phrase_is_inserted_once(monkeypatch):
    svc = _service()
    asr = _Asr(["hello how", "how are you", "fine thanks"])
    svc.asr = asr
    svc.state = STATE_RECORDING
    inserts = _fake_insert(monkeypatch)

    assert svc._on_audio_chunk(np.full(3 * SR, 0.1, dtype=np.float32))
    assert svc._on_audio_chunk(np.full(3 * SR, 0.1, dtype=np.float32))
    worker = svc._finish_chunks()
    svc.state = STATE_PROCESSING

    svc._transcribe_worker(np.zeros(SR, dtype=np.float32), _TailRecorder(),
                           worker)

    assert inserts == ["hello how are you fine thanks"]
    assert [call[2] for call in asr.calls] == [False, False, False]
    assert [call[1] for call in asr.calls] == [False, False, True]
    assert svc.live["state"] == STATE_IDLE


def test_a_pending_chunk_keeps_the_phrase_when_the_tail_is_empty(monkeypatch):
    svc = _service()
    svc.asr = _Asr(["only the chunk"])
    svc.state = STATE_RECORDING
    inserts = _fake_insert(monkeypatch)

    assert svc._on_audio_chunk(np.full(2 * SR, 0.1, dtype=np.float32))
    worker = svc._finish_chunks()
    svc.state = STATE_PROCESSING

    svc._transcribe_worker(None, _TailRecorder(), worker)

    assert inserts == ["only the chunk"]
    assert svc.live["state"] == STATE_IDLE


class _GatedAsr:
    """Blocks inside transcribe until the test opens the gate."""

    def __init__(self, text="CANCELLED PHRASE"):
        self.text = text
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.calls = 0
        self.last_segments = []

    def transcribe(self, samples, verbose=True, *, partial=False):
        self.calls += 1
        self.entered.set()
        assert self.gate.wait(5.0)
        return self.text


def test_a_cancelled_take_never_writes_into_the_next_one():
    svc = _service()
    asr = _GatedAsr()
    svc.asr = asr
    svc.state = STATE_RECORDING
    assert svc._on_audio_chunk(np.full(SR, 0.1, dtype=np.float32))
    assert asr.entered.wait(5.0)

    # Esc while the piece is still being recognized: the queue closes, the
    # phrase is dropped, and the next take resets its registers at once.
    worker = svc._finish_chunks()
    svc.state = STATE_IDLE
    svc._reset_chunks()
    svc.state = STATE_RECORDING
    asr.gate.set()

    assert worker is not None
    worker.join(5.0)
    assert asr.calls == 1
    assert svc.live.get("partial", "") == ""
    assert svc._chunk_texts == []
    assert svc._chunk_segments == []
