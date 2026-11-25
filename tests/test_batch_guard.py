"""Batched path quality thresholds (B5): a failing segment goes sequential.

faster-whisper 1.2.1's ``BatchedInferencePipeline`` accepts
``compression_ratio_threshold``, ``log_prob_threshold`` and
``no_speech_threshold`` and applies none of them. The guard in
``ASR.transcribe_array`` checks every batched segment against the values the
sequential call uses and decodes a failing one again on the sequential path;
without the guard the batched text would pass silently.
"""
from __future__ import annotations

import asr
import hallucination
import numpy as np
from transcript import Segment


class _Log:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def debug(self, fmt, *args):
        pass

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)


class _Info:
    language = "en"


class _Row:
    """A faster-whisper segment stand-in carrying the four readings."""

    def __init__(self, segment, **extra):
        self.start = getattr(segment, "start", 0.0)
        self.end = getattr(segment, "end", 0.0)
        self.text = getattr(segment, "text", "")
        self.words = getattr(segment, "words", None)
        for name in ("avg_logprob", "compression_ratio", "no_speech_prob",
                     "temperature"):
            setattr(self, name, getattr(segment, name, None))
        for key, value in extra.items():
            setattr(self, key, value)


class _Pipeline:
    """Stands in for faster_whisper.BatchedInferencePipeline.

    ``batches`` holds one list of rows per ``transcribe`` call, so a
    multi-block test controls every block.
    """

    batches: list[list[Segment]] = []
    instances: list[_Pipeline] = []

    def __init__(self, model):
        self.model = model
        self.calls = []
        _Pipeline.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append({"samples": audio.shape[0], **kwargs})
        index = len(self.calls) - 1
        rows = _Pipeline.batches[index] if index < len(_Pipeline.batches) else []

        def stream():
            for row in rows:
                yield _Row(row)

        return stream(), _Info()


class _SeqModel:
    """Stands in for WhisperModel: the sequential retry reads one slice."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, dict(kwargs)))
        rows = self.rows if self.rows is not None else [
            Segment(0.0, audio.shape[0] / 16000.0, " sequential take")]

        def stream():
            for row in rows:
                yield _Row(row)

        return stream(), _Info()


def _engine(monkeypatch, batches, seq_rows, language="en"):
    _Pipeline.batches = batches
    _Pipeline.instances.clear()
    monkeypatch.setattr(asr, "BatchedInferencePipeline", _Pipeline)
    engine = asr.ASR({"language": language, "model": "fake", "hotwords": "",
                      "dictionary": []}, _Log())
    engine.model = _SeqModel(seq_rows)
    return engine


def _audio(seconds: float) -> np.ndarray:
    return np.zeros((int(seconds * asr.TARGET_RATE),), dtype=np.float32)


def _warned(engine, needle):
    return any(level == "warning" and needle in line
               for level, line in engine.log.lines)


# -- the ratio threshold ------------------------------------------------
def test_a_segment_over_the_ratio_threshold_is_redecoded_sequentially(monkeypatch):
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " batched suspicious", compression_ratio=1.9),
          Segment(1.0, 2.0, " batched fine", compression_ratio=1.1)]],
        [Segment(0.0, 1.0, " sequential verdict")])

    segments = list(engine.transcribe_array(_audio(2.0), 2.0, batched=True))

    # The failing batched text is replaced, the passing one is untouched.
    assert [s.text for s in segments] == ["sequential verdict", "batched fine"]
    assert [round(s.start, 2) for s in segments] == [0.0, 1.0]
    assert len(engine.model.calls) == 1
    slice_, kwargs = engine.model.calls[0]
    assert slice_.shape == (16000,)
    assert kwargs["word_timestamps"] is False
    assert _warned(engine, "compression_ratio 1.90 > 1.35")
    assert _warned(engine, "re-recognizing it on the sequential path")


def test_a_segment_within_the_ratio_threshold_keeps_the_batched_result(monkeypatch):
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " batched take", compression_ratio=1.2)]],
        None)

    segments = list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    assert [s.text for s in segments] == ["batched take"]
    assert engine.model.calls == []
    assert not _warned(engine, "failed a threshold")


def test_the_calibrated_ratio_holds_only_for_its_language(monkeypatch):
    # The F0 measurement: clean Russian speech reads 1.476, so 1.35 there
    # would re-decode every window; the library default is what sequential
    # uses outside the calibrated language.
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " clean russian speech sample",
                  compression_ratio=1.476, avg_logprob=-0.25,
                  no_speech_prob=0.02)]],
        None, language="ru")

    segments = list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    assert [s.text for s in segments] == ["clean russian speech sample"]
    assert engine.model.calls == []


def test_the_default_ratio_still_guards_the_other_languages(monkeypatch):
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " batched repetitive", compression_ratio=2.6)]],
        [Segment(0.0, 1.0, " sequential verdict")], language="ru")

    segments = list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    assert [s.text for s in segments] == ["sequential verdict"]
    assert len(engine.model.calls) == 1
    assert _warned(engine, "compression_ratio 2.60 > 2.4")


# -- the logprob and no-speech thresholds ------------------------------
def test_a_low_logprob_segment_goes_to_the_sequential_path(monkeypatch):
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " batched unsure", avg_logprob=-1.4,
                  compression_ratio=1.0, no_speech_prob=0.1)]],
        [Segment(0.0, 1.0, " sequential sure")])

    segments = list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    assert [s.text for s in segments] == ["sequential sure"]
    assert len(engine.model.calls) == 1
    assert _warned(engine, "avg_logprob -1.40 < -1")


def test_a_quiet_window_is_left_to_the_sequential_silence_verdict(monkeypatch):
    # Sequential skips a window whose no_speech is high and whose logprob is
    # low; the retry lets that verdict stand and the batched text disappears.
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " batched invented", avg_logprob=-1.6,
                  compression_ratio=1.0, no_speech_prob=0.9)]],
        [])

    segments = list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    assert segments == []
    assert len(engine.model.calls) == 1
    assert _warned(engine, "no_speech_prob 0.90 > 0.6")
    assert any(level == "info" and "Sequential retry" in line
               and "no speech" in line
               for level, line in engine.log.lines)


def test_a_quiet_window_the_engine_still_reads_as_speech_is_kept(monkeypatch):
    # no_speech alone is not a failure: sequential keeps a window whose
    # logprob is fine (only the library's paired silence rule skips).
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " quiet but real", avg_logprob=-0.4,
                  compression_ratio=1.0, no_speech_prob=0.9)]],
        None)

    segments = list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    assert [s.text for s in segments] == ["quiet but real"]
    assert engine.model.calls == []


# -- the seam with the block loop --------------------------------------
def test_the_retry_reads_the_segment_span_inside_its_own_block(monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " first block")],
         [Segment(0.0, 0.5, " no good", compression_ratio=1.9),
          Segment(0.5, 1.0, " also fine")],
         [Segment(0.0, 1.0, " third block")]],
        [Segment(0.0, 0.5, " fixed span")])

    segments = list(engine.transcribe_array(_audio(3.0), 3.0, batched=True,
                                            word_timestamps=True))

    assert [s.text for s in segments] == ["first block", "fixed span",
                                          "also fine", "third block"]
    assert [round(s.start, 2) for s in segments] == [0.0, 1.0, 1.5, 2.0]
    # The retry receives only the failing span, never the whole block.
    slice_, kwargs = engine.model.calls[0]
    assert slice_.shape == (8000,)
    assert kwargs["word_timestamps"] is True
    assert kwargs["initial_prompt"] == _Pipeline.instances[0].calls[1][
        "initial_prompt"]


def test_the_guard_keeps_progress_monotonic_and_final(monkeypatch):
    engine = _engine(
        monkeypatch,
        [[Segment(0.0, 1.0, " batched", compression_ratio=1.9),
          Segment(1.0, 2.0, " batched two")]],
        [])
    progress = []

    list(engine.transcribe_array(_audio(2.0), 2.0, batched=True,
                                 on_progress=progress.append))

    assert progress == sorted(progress)
    assert progress[-1] == 100.0


def test_the_batched_call_declares_the_same_thresholds(monkeypatch):
    engine = _engine(monkeypatch,
                     [[Segment(0.0, 1.0, " en take")]], None)

    list(engine.transcribe_array(_audio(1.0), 1.0, batched=True))

    kwargs = _Pipeline.instances[0].calls[0]
    assert (kwargs["compression_ratio_threshold"]
            == hallucination.COMPRESSION_RATIO_THRESHOLD)
    assert kwargs["log_prob_threshold"] == asr.LOG_PROB_THRESHOLD
    assert kwargs["no_speech_threshold"] == asr.NO_SPEECH_THRESHOLD
