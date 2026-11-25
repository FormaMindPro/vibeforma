"""File mode: the seam between 600 s blocks carries the previous ending.

Every block is a fresh engine call, so without a hint a name or a style
decided in the first block drifts at 600 s and again at 1200 s. The tail of
the block just decoded travels as the next block's ``initial_prompt``: the
last ``FILE_CONTEXT_TAIL_CHARS`` characters, never the whole block, and it
replaces itself instead of accumulating.
"""
from __future__ import annotations

import asr
import pytest
from transcript import Segment

np = pytest.importorskip("numpy")

BASE = asr.PROMPT_PRESETS["ru"]


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
        self._add("debug", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)


class _Row:
    """A tiny stand-in for faster-whisper's segment and info objects."""

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


class _BlockModel:
    """One text per block: the test says what each block recognized."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def transcribe(self, audio, **kwargs):
        index = len(self.calls)
        self.calls.append({"samples": audio.shape[0], **kwargs})
        text = self.texts[index] if index < len(self.texts) else ""

        def rows():
            yield _Row(Segment(0.0, audio.shape[0] / 16000.0, " " + text))

        return rows(), _Row(None, language="ru")


# The first block is longer than the tail bound on purpose: the seam must
# carry its ending, not the block. The name sits in the ending.
FIRST_BLOCK = (
    "At the beginning the talk wanders through the weather, the traffic and "
    "a dozen other matters that nobody will remember later, because a long "
    "meeting always drifts away from its subject, and then the speaker "
    "finally names the product: Forma."
)
SECOND_BLOCK = (
    "The name Forma comes back in the second block and the speaker keeps "
    "going."
)


def _engine(texts):
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, _Log())
    engine.model = _BlockModel(texts)
    return engine


def _run(engine, seconds=2.5, **kwargs):
    audio = np.zeros((int(seconds * 16000),), dtype=np.float32)
    return list(engine.transcribe_array(audio, seconds, **kwargs))


# -- the tail itself ---------------------------------------------------
def test_tail_is_bounded_and_cut_at_a_word_start():
    assert asr.file_context_tail("") == ""
    assert asr.file_context_tail("  short \n words  ") == "short words"
    assert asr.file_context_tail(FIRST_BLOCK) == asr.file_context_tail(
        FIRST_BLOCK, asr.FILE_CONTEXT_TAIL_CHARS)

    tail = asr.file_context_tail(FIRST_BLOCK)
    assert 0 < len(tail) <= asr.FILE_CONTEXT_TAIL_CHARS
    assert FIRST_BLOCK.endswith(tail)
    assert "Forma" in tail

    assert asr.file_context_tail("alpha beta gamma", 11) == "beta gamma"
    assert asr.file_context_tail(FIRST_BLOCK, 0) == ""
    assert asr.file_context_tail(FIRST_BLOCK, -5) == ""


# -- the sequential path -----------------------------------------------
def test_the_second_block_hint_carries_the_first_tail_not_the_whole_block(
        monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    engine = _engine([FIRST_BLOCK, SECOND_BLOCK, "and the third block ends."])

    segments = _run(engine)

    calls = engine.model.calls
    assert [call["samples"] for call in calls] == [16000, 16000, 8000]
    assert [s.text for s in segments] == [FIRST_BLOCK, SECOND_BLOCK,
                                          "and the third block ends."]

    first_tail = asr.file_context_tail(FIRST_BLOCK)
    second_tail = asr.file_context_tail(SECOND_BLOCK)

    # The first block starts the job: it keeps the configured preset alone.
    assert calls[0]["initial_prompt"] == BASE

    # The second block sees the ending of the first — and not the whole of
    # it, so the prompt stays bounded.
    second_hint = calls[1]["initial_prompt"]
    assert second_hint.startswith(BASE)
    assert first_tail in second_hint
    assert FIRST_BLOCK not in second_hint
    assert "At the beginning" not in second_hint
    assert second_hint.count(first_tail) == 1
    assert len(second_hint) <= len(BASE) + 1 + asr.FILE_CONTEXT_TAIL_CHARS

    # The name survives into the second and the third block: the third hint
    # carries the second block's ending, and the first block's tail is gone
    # instead of piling up.
    third_hint = calls[2]["initial_prompt"]
    assert third_hint.startswith(BASE)
    assert second_tail in third_hint
    assert "Forma" in third_hint
    assert first_tail not in third_hint
    assert FIRST_BLOCK not in third_hint
    assert len(third_hint) <= len(BASE) + 1 + asr.FILE_CONTEXT_TAIL_CHARS


def test_without_the_tail_the_seam_loses_the_name(monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    monkeypatch.setattr(asr, "FILE_CONTEXT_TAIL_CHARS", 0)
    engine = _engine([FIRST_BLOCK, SECOND_BLOCK, "and the third block ends."])

    _run(engine)

    prompts = [call["initial_prompt"] for call in engine.model.calls]
    assert prompts == [BASE, BASE, BASE]
    assert all("Forma" not in prompt for prompt in prompts)


def test_a_job_prompt_stays_the_base_and_the_tail_follows_it(monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    engine = _engine([FIRST_BLOCK, SECOND_BLOCK])

    _run(engine, initial_prompt="names: Forma", seconds=2.0)

    first_hint = engine.model.calls[0]["initial_prompt"]
    second_hint = engine.model.calls[1]["initial_prompt"]
    assert first_hint == "names: Forma"
    assert second_hint == "names: Forma " + asr.file_context_tail(FIRST_BLOCK)


def test_a_silent_block_keeps_the_last_tail(monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    engine = _engine([FIRST_BLOCK, "   ", "the third block starts again"])

    _run(engine)

    third_hint = engine.model.calls[2]["initial_prompt"]
    assert asr.file_context_tail(FIRST_BLOCK) in third_hint


def test_a_clip_gap_does_not_inherit_the_previous_piece(monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    engine = _engine([FIRST_BLOCK, "another piece", "the last piece"])

    list(engine.transcribe_array(
        np.zeros((5 * 16000,), dtype=np.float32), 5.0,
        clip_timestamps="0,1,3,5"))

    prompts = [call["initial_prompt"] for call in engine.model.calls]
    assert prompts[0] == BASE
    # 0-1 s and 3-4 s are not neighbours: no tail crosses the gap.
    assert prompts[1] == BASE
    # Inside the second range the seam works again.
    assert asr.file_context_tail("another piece") in prompts[2]


# -- the batched path --------------------------------------------------
class _RecordingPipeline:
    instances = []

    def __init__(self, model):
        self.model = model
        self.calls = []
        _RecordingPipeline.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append({"samples": audio.shape[0], **kwargs})
        index = len(self.calls) - 1
        texts = _RecordingPipeline.texts
        text = texts[index] if index < len(texts) else ""

        def rows():
            yield _Row(Segment(0.0, audio.shape[0] / 16000.0, " " + text))

        return rows(), _Row(None, language="ru")


def test_the_batched_path_carries_the_same_tail(monkeypatch):
    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    monkeypatch.setattr(asr, "BatchedInferencePipeline", _RecordingPipeline)
    _RecordingPipeline.instances.clear()
    _RecordingPipeline.texts = [FIRST_BLOCK, SECOND_BLOCK,
                                "and the third block ends."]
    engine = _engine([])

    _run(engine, batched=True)

    assert engine.model.calls == []
    pipeline = _RecordingPipeline.instances[0]
    assert [call["samples"] for call in pipeline.calls] == [
        16000, 16000, 8000]
    hints = [call["initial_prompt"] for call in pipeline.calls]
    assert hints[0] == BASE
    assert asr.file_context_tail(FIRST_BLOCK) in hints[1]
    assert FIRST_BLOCK not in hints[1]
    assert asr.file_context_tail(SECOND_BLOCK) in hints[2]
    assert FIRST_BLOCK not in hints[2]
    assert all(call["condition_on_previous_text"] is False
               for call in pipeline.calls)
