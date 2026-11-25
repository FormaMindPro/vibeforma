"""Recognition confidence: kept on the segment, published, and acted upon.

Card-238 closes finding B10: ``avg_logprob``, ``compression_ratio``,
``no_speech_prob`` and ``temperature`` used to be read and thrown away, and a
word's ``probability`` was not part of the shape at all. Now the JSON formats
publish all five, and the dictation path treats a low-confidence phrase as a
draft to review rather than a blind paste. ``temperature > 0`` is a trigger of
its own: the window's first decode already failed, whatever the retry says.
"""
import json
import logging
import sys
import types

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first
import transcript  # noqa: E402
from transcript import Segment, Word  # noqa: E402
from vibeforma.service import Service  # noqa: E402

SAMPLE_RATE = 16000


class RecordingLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def debug(self, fmt, *args):
        self._add("debug", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)


class _Info:
    language = "en"


class _Word:
    """A faster-whisper word stand-in: its own probability is the point."""

    def __init__(self, word, probability, start=0.0, end=0.4):
        self.word = word
        self.probability = probability
        self.start = start
        self.end = end


class _EngineSegment:
    """A faster-whisper segment stand-in with all four readings."""

    def __init__(self, text, *, words=None, avg_logprob=-0.2,
                 compression_ratio=1.1, no_speech_prob=0.05,
                 temperature=0.0):
        self.seek = 0
        self.start = 0.0
        self.end = 1.0
        self.text = text
        self.words = words
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio
        self.no_speech_prob = no_speech_prob
        self.temperature = temperature


class _Model:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, _audio, **kwargs):
        self.calls.append(kwargs)
        return list(self.segments), _Info()


def _engine(segments):
    engine = asr.ASR({"language": "en", "hotwords": "", "dictionary": []},
                     logging.getLogger("test_confidence"))
    model = _Model(segments)
    engine.model = model
    return engine, model


def _audio():
    return np.zeros(SAMPLE_RATE, dtype=np.float32)


# -- the readings survive the engine ---------------------------------------

def test_dictation_keeps_all_five_readings_on_last_segments():
    engine, model = _engine([
        _EngineSegment(" hello", words=[_Word(" hello", 0.31)],
                       avg_logprob=-0.42, compression_ratio=1.3,
                       no_speech_prob=0.11, temperature=0.0),
    ])

    text = engine.transcribe(_audio())

    assert text == "hello"
    segment = engine.last_segments[0]
    assert segment.avg_logprob == -0.42
    assert segment.compression_ratio == 1.3
    assert segment.no_speech_prob == 0.11
    assert segment.temperature == 0.0
    assert segment.words is not None
    assert segment.words[0].probability == 0.31
    assert model.calls[-1]["word_timestamps"] is True


def test_a_partial_never_replaces_the_last_take():
    engine, _model = _engine([_EngineSegment(" hello")])
    engine.transcribe(_audio())
    take = engine.last_segments

    engine.transcribe(_audio(), verbose=False, partial=True)

    assert engine.last_segments is take


# -- the readings reach the JSON formats -----------------------------------

def test_json_publishes_the_five_readings():
    segments = [Segment(0.0, 1.0, " hello",
                        words=[Word(0.0, 0.4, " hello", 0.31)],
                        avg_logprob=-0.42, compression_ratio=1.3,
                        no_speech_prob=0.11, temperature=0.2)]

    payload = json.loads(transcript.to_json(segments, {"language": "en"}))
    row = payload["segments"][0]

    assert row["avg_logprob"] == -0.42
    assert row["compression_ratio"] == 1.3
    assert row["no_speech_prob"] == 0.11
    assert row["temperature"] == 0.2
    assert row["words"][0]["probability"] == 0.31
    assert row["text"] == "hello"


# -- the review rule -------------------------------------------------------

def test_a_confident_segment_needs_no_review():
    segment = Segment(0.0, 1.0, "hello", avg_logprob=-0.18,
                      compression_ratio=1.1, no_speech_prob=0.02,
                      temperature=0.0)

    assert transcript.needs_review([segment]) is False


def test_low_logprob_needs_review():
    segment = Segment(0.0, 1.0, "hello", avg_logprob=-1.4,
                      compression_ratio=1.1, no_speech_prob=0.02,
                      temperature=0.0)

    assert transcript.needs_review([segment]) is True


def test_noise_needs_review():
    segment = Segment(0.0, 1.0, "hello", avg_logprob=-0.2,
                      compression_ratio=1.1, no_speech_prob=0.91,
                      temperature=0.0)

    assert transcript.needs_review([segment]) is True


def test_a_repeating_window_needs_review():
    segment = Segment(0.0, 1.0, "hello", avg_logprob=-0.2,
                      compression_ratio=3.4, no_speech_prob=0.02,
                      temperature=0.0)

    assert transcript.needs_review([segment]) is True


def test_temperature_above_zero_needs_review_even_with_a_good_logprob():
    # A fallback decode means the first pass failed its own checks; the retry
    # looking confident must not clear the window.
    segment = Segment(0.0, 1.0, "hello", avg_logprob=-0.05,
                      compression_ratio=1.1, no_speech_prob=0.02,
                      temperature=0.2)

    assert transcript.needs_review([segment]) is True


def test_uncertain_words_names_only_the_doubtful_ones():
    segments = [Segment(0.0, 1.0, " clear murky",
                        words=[Word(0.0, 0.4, " clear", 0.97),
                               Word(0.4, 0.9, " murky", 0.24)])]

    assert transcript.uncertain_words(segments) == ["murky"]


# -- the service: a noisy phrase is a draft, a confident one is a paste -----

class FakeRecorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class FakeASR:
    def __init__(self, text, segments):
        self.text = text
        self.last_segments = segments

    def transcribe(self, _buf):
        return self.text


def _make_service():
    cfg = {
        "locale": "en",
        "dictionary": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
    }
    svc = Service(cfg, RecordingLog())
    svc._running = True
    return svc


def _fake_insert(monkeypatch):
    calls = []
    fake = types.ModuleType("insert")
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.get_foreground_hwnd = lambda: 0
    fake.insert_text = lambda text, **kwargs: (
        calls.append(text) or "inserted")
    monkeypatch.setitem(sys.modules, "insert", fake)
    return calls


def test_a_noisy_phrase_waits_in_the_draft_instead_of_a_blind_paste(
        monkeypatch):
    svc = _make_service()
    svc.asr = FakeASR("unclear phrase", [
        Segment(0.0, 1.0, "unclear phrase",
                words=[Word(0.0, 0.4, "unclear", 0.25)],
                avg_logprob=-1.4, compression_ratio=1.2,
                no_speech_prob=0.7, temperature=0.0),
    ])
    svc.state = "processing"
    inserts = _fake_insert(monkeypatch)

    svc._transcribe_worker(object(), FakeRecorder())

    assert inserts == []
    assert svc.live["draft"] == "unclear phrase"
    assert svc.live["draft_uncertain"] == ["unclear"]
    assert svc.live["state"] == "idle"


def test_a_confident_phrase_is_pasted_as_before(monkeypatch):
    svc = _make_service()
    svc.asr = FakeASR("hello there", [
        Segment(0.0, 1.0, "hello there",
                words=[Word(0.0, 0.4, "hello", 0.98),
                       Word(0.4, 1.0, "there", 0.96)],
                avg_logprob=-0.18, compression_ratio=1.1,
                no_speech_prob=0.02, temperature=0.0),
    ])
    svc.state = "processing"
    inserts = _fake_insert(monkeypatch)

    svc._transcribe_worker(object(), FakeRecorder())

    assert inserts == ["hello there"]
    assert svc.live["draft"] == ""
    assert svc.live["state"] == "idle"
