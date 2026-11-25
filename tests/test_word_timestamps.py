"""Word timestamps survive the library's alignment crash (issue #1342).

faster-whisper 1.2.1 can raise ``IndexError`` from ``find_alignment`` while
the segment iterator is being consumed (a trailing sliver of speech). A file
job with ``file_word_timestamps`` on must still produce text: the block is
decoded again without word timestamps, the retry is logged, and the result of
that block carries no words. With the setting off nothing changes: the error
is not hidden.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first

ROOT = Path(__file__).resolve().parents[1]
TEST_WAV = ROOT / "test_ru.wav"
CRASH = "index 5 is out of bounds for axis 0 with size 5"
ROWS = ((0.0, 1.0, " first"), (1.0, 2.0, " second"), (2.0, 3.0, " tail"))


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
    language = "ru"
    duration = 3.0


class _Word:
    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word
        self.probability = 0.9


class _Row:
    """A faster-whisper segment stand-in; words only when alignment ran."""

    def __init__(self, start, end, text, words=None):
        self.seek = 0
        self.start = start
        self.end = end
        self.text = text
        self.words = words
        self.avg_logprob = -0.2
        self.compression_ratio = 1.1
        self.no_speech_prob = 0.0
        self.temperature = 0.0


def _rows(with_words: bool):
    return [
        _Row(start, end, text,
             [_Word(start, end, text)] if with_words else None)
        for start, end, text in ROWS
    ]


class _AlignCrashModel:
    """Raises the issue-#1342 IndexError once word timestamps are consumed."""

    def __init__(self, crash_after: int = 0):
        self.crash_after = crash_after
        self.calls = []

    def transcribe(self, _audio, **kwargs):
        self.calls.append(dict(kwargs))
        word_ts = bool(kwargs.get("word_timestamps"))
        rows = _rows(word_ts)
        crash_after = self.crash_after

        def stream():
            for index, row in enumerate(rows):
                if word_ts and index >= crash_after:
                    raise IndexError(CRASH)
                yield row

        return stream(), _Info()


class _CrashingIterator:
    def __iter__(self):
        return self

    def __next__(self):
        raise IndexError(CRASH)


class _BrokenModel:
    """Crashes on every call: the retry cannot save a real failure."""

    def transcribe(self, _audio, **_kwargs):
        return _CrashingIterator(), _Info()


def _engine(model):
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, _Log())
    engine.model = model
    return engine


def _warned(engine, needle: str) -> bool:
    return any(needle in line for _level, line in engine.log.lines)


def _wait(job, timeout=10.0):
    import file_jobs

    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in file_jobs.TERMINAL:
            return job.state
        time.sleep(0.02)
    return job.state


# -- the guard -------------------------------------------------------------
@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_a_file_with_a_trailing_sliver_still_yields_text():
    model = _AlignCrashModel(crash_after=0)
    engine = _engine(model)

    segments = list(engine.transcribe_file(str(TEST_WAV), language="ru",
                                           word_timestamps=True))

    assert [s.text for s in segments] == ["first", "second", "tail"]
    assert [call["word_timestamps"] for call in model.calls] == [True, False]
    assert all(segment.words is None for segment in segments)
    assert _warned(engine, "retrying the block without word timestamps")


def test_the_retried_block_does_not_repeat_what_was_yielded():
    model = _AlignCrashModel(crash_after=1)
    engine = _engine(model)

    segments = list(engine.transcribe_array(
        np.zeros(3 * 16000, dtype=np.float32), 3.0, word_timestamps=True))

    assert [s.text for s in segments] == ["first", "second", "tail"]
    assert [(s.start, s.end) for s in segments] == [
        (0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    assert model.calls[1]["word_timestamps"] is False


def test_without_word_timestamps_the_crash_is_not_hidden():
    engine = _engine(_BrokenModel())

    with pytest.raises(IndexError):
        list(engine.transcribe_array(
            np.zeros(16000, dtype=np.float32), 1.0, word_timestamps=False))

    assert not _warned(engine, "without word timestamps")


def test_a_crash_that_survives_the_retry_still_fails_the_job():
    engine = _engine(_BrokenModel())

    with pytest.raises(IndexError):
        list(engine.transcribe_array(
            np.zeros(16000, dtype=np.float32), 1.0, word_timestamps=True))

    assert _warned(engine, "retrying the block without word timestamps")


def test_the_batched_path_retries_without_word_timestamps(monkeypatch):
    instances = []

    class _CrashingPipeline:
        def __init__(self, model):
            self.model = model
            self.calls = []
            instances.append(self)

        def transcribe(self, _audio, **kwargs):
            self.calls.append(dict(kwargs))
            word_ts = bool(kwargs.get("word_timestamps"))
            rows = _rows(word_ts)

            def stream():
                for index, row in enumerate(rows):
                    if word_ts and index >= 1:
                        raise IndexError(CRASH)
                    yield row

            return stream(), _Info()

    monkeypatch.setattr(asr, "BatchedInferencePipeline", _CrashingPipeline)
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, _Log())
    engine.model = object()

    segments = list(engine.transcribe_array(
        np.zeros(3 * 16000, dtype=np.float32), 3.0, word_timestamps=True,
        batched=True))

    assert [s.text for s in segments] == ["first", "second", "tail"]
    assert [call["word_timestamps"] for call in instances[0].calls] == [
        True, False]
    assert _warned(engine, "retrying the block without word timestamps")


# -- the setting and the job ----------------------------------------------
@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_a_file_job_with_the_setting_on_ends_done(tmp_path):
    import file_jobs

    model = _AlignCrashModel(crash_after=0)
    engine = _engine(model)
    queue = file_jobs.FileJobs(
        {"language": "ru", "model": "fake", "file_max_mb": 0,
         "job_keep_s": 3600, "file_roots": [], "file_word_timestamps": True},
        engine, _Log())
    try:
        job = queue.submit(str(TEST_WAV), formats=["txt"], out=str(tmp_path),
                           trusted=True)
        assert _wait(job) == "done"
    finally:
        queue.shutdown()

    assert [call["word_timestamps"] for call in model.calls] == [True, False]
    written = (tmp_path / "test_ru.txt").read_text(encoding="utf-8")
    assert written.split() == ["first", "second", "tail"]
