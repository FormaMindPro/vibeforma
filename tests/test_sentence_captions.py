"""Subtitle cues follow sentences, not the engine's 30-second window.

The engine cuts its segments at window boundaries, so a file transcribed with
word timestamps still writes one SRT cue per window. The cue cut reads the
words instead: a sentence terminator closes a cue, a pause at or above the
threshold closes it too, and a shorter gap glues the words, across windows
and across 600-second blocks. Without word timestamps nothing is re-cut and
the engine's segments stand exactly as before.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("av")

import asr  # noqa: E402 - asr imports numpy; skip the module first
import transcript  # noqa: E402
from transcript import Segment, Word  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEST_WAV = ROOT / "test_ru.wav"


def _w(start, end, text):
    """A word the way the engine hands it over: its own leading space."""
    return Word(start=start, end=end, text=text)


def _gap(gap_s):
    """One engine window whose two words are ``gap_s`` seconds apart."""
    return [Segment(0.0, 1.0 + gap_s + 1.0, "",
                    words=[_w(0.0, 1.0, " first"),
                           _w(1.0 + gap_s, 2.0 + gap_s, " second")])]


# -- the cut itself --------------------------------------------------------
def test_a_pause_above_the_threshold_closes_the_cue():
    cues = transcript.sentence_captions(
        _gap(transcript.CAPTION_PAUSE_S + 0.01))

    assert [cue.text for cue in cues] == ["first", "second"]
    pause_end = 1.0 + transcript.CAPTION_PAUSE_S + 0.01
    assert [cue.start for cue in cues] == pytest.approx([0.0, pause_end])
    assert [cue.end for cue in cues] == pytest.approx([1.0, pause_end + 1.0])


def test_a_pause_below_the_threshold_keeps_one_cue():
    cues = transcript.sentence_captions(
        _gap(transcript.CAPTION_PAUSE_S - 0.01))

    assert [cue.text for cue in cues] == ["first second"]


def test_a_long_pause_splits_even_without_a_terminator():
    segments = [Segment(0.0, 3.0, "",
                        words=[_w(0.2, 0.6, " first"),
                               _w(0.6, 1.0, " phrase"),
                               _w(2.2, 2.6, " second"),
                               _w(2.6, 3.0, " phrase")])]

    cues = transcript.sentence_captions(segments)

    assert [cue.text for cue in cues] == ["first phrase", "second phrase"]
    assert [(cue.start, cue.end) for cue in cues] == [(0.2, 1.0), (2.2, 3.0)]


def test_a_sentence_end_closes_the_cue_without_any_pause():
    segments = [Segment(0.0, 2.0, "",
                        words=[_w(0.2, 0.6, " first"),
                               _w(0.6, 1.0, " one."),
                               _w(1.05, 1.45, " second"),
                               _w(1.45, 1.8, " two.")])]

    cues = transcript.sentence_captions(segments)

    assert [cue.text for cue in cues] == ["first one.", "second two."]


def test_a_closing_quote_after_the_terminator_still_closes():
    segments = [Segment(0.0, 2.0, "",
                        words=[_w(0.2, 0.8, " he"),
                               _w(0.8, 1.2, " said"),
                               _w(1.2, 1.6, " \u00abyes.\u00bb"),
                               _w(1.62, 2.0, " fine")])]

    cues = transcript.sentence_captions(segments)

    assert [cue.text for cue in cues] == ["he said \u00abyes.\u00bb", "fine"]


def test_a_seam_between_engine_windows_glues_one_sentence():
    first = Segment(0.0, 2.9, "",
                    words=[_w(0.0, 1.4, " first"), _w(1.4, 2.9, " one")])
    second = Segment(2.9, 6.0, "",
                     words=[_w(2.9, 4.2, " second"), _w(4.2, 5.8, " two")])

    cues = transcript.sentence_captions([first, second])

    assert [cue.text for cue in cues] == ["first one second two"]
    assert [(cue.start, cue.end) for cue in cues] == [(0.0, 5.8)]


def test_a_pause_at_the_seam_still_closes_the_cue():
    first = Segment(0.0, 2.0, "", words=[_w(0.0, 1.0, " first")])
    second = Segment(2.0, 4.0, "", words=[_w(2.5, 3.5, " second")])

    cues = transcript.sentence_captions([first, second])

    assert [cue.text for cue in cues] == ["first", "second"]


def test_a_wordless_segment_keeps_its_own_cue():
    first = Segment(0.0, 1.0, "", words=[_w(0.0, 1.0, " first")])
    # A block whose alignment was retried carries no words (card-239).
    middle = Segment(1.0, 2.0, " no words here")
    last = Segment(2.0, 3.0, "", words=[_w(2.0, 3.0, " third")])

    cues = transcript.sentence_captions([first, middle, last])

    assert [cue.text for cue in cues] == ["first", "no words here", "third"]


def test_cues_stay_non_negative_and_never_overlap():
    segments = [Segment(0.0, 2.0, "",
                        words=[_w(-2.0, -1.0, " before"),
                               _w(0.0, 1.0, " one"),
                               _w(0.8, 1.2, " two. "),
                               _w(1.3, 1.6, " three")])]

    cues = transcript.sentence_captions(segments)

    previous = 0.0
    for cue in cues:
        assert cue.start >= previous
        assert cue.end >= cue.start
        previous = cue.end


def test_the_threshold_is_a_parameter():
    segments = _gap(0.4)

    assert len(transcript.sentence_captions(segments)) == 1
    assert len(transcript.sentence_captions(segments, pause_s=0.1)) == 2


# -- the formats -----------------------------------------------------------
def test_srt_numbers_the_sentence_cues():
    segments = [Segment(0.0, 2.0, "",
                        words=[_w(0.2, 0.6, " first"),
                               _w(0.6, 1.0, " one."),
                               _w(1.05, 1.45, " second"),
                               _w(1.45, 1.8, " two.")])]

    body = transcript.to_srt(segments)

    assert body == ("1\n00:00:00,200 --> 00:00:01,000\nfirst one.\n"
                    "\n"
                    "2\n00:00:01,050 --> 00:00:01,800\nsecond two.\n")


def test_vtt_carries_the_same_cut():
    segments = [Segment(0.0, 2.0, "",
                        words=[_w(0.2, 0.6, " first"),
                               _w(0.6, 1.0, " one."),
                               _w(1.05, 1.45, " second"),
                               _w(1.45, 1.8, " two.")])]

    body = transcript.to_vtt(segments)

    assert body.startswith("WEBVTT\n\n00:00:00.200 --> 00:00:01.000\n"
                           "first one.\n")
    assert "00:00:01.050 --> 00:00:01.800\nsecond two.\n" in body


def test_without_words_the_engine_segments_stand():
    segments = [Segment(0.26, 6.7, " first phrase"),
                Segment(6.7, 11.66, " second phrase")]

    cues = transcript.sentence_captions(segments)

    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (0.26, 6.7, "first phrase"), (6.7, 11.66, "second phrase")]
    assert transcript.to_srt(segments).startswith(
        "1\n00:00:00,260 --> 00:00:06,700\nfirst phrase\n")


def test_an_empty_segment_is_still_dropped_without_words():
    segments = [Segment(0.0, 1.0, " one"), Segment(1.0, 2.0, "   "),
                Segment(2.0, 3.0, " two")]

    assert [cue.text for cue in transcript.sentence_captions(segments)] == [
        "one", "two"]


def test_json_keeps_the_engine_segments():
    segment = Segment(0.0, 2.0, " first. second",
                      words=[_w(0.2, 1.0, " first."), _w(1.1, 1.9, " second")])

    payload = json.loads(transcript.to_json([segment], {}))

    assert len(payload["segments"]) == 1
    assert payload["segments"][0]["text"] == "first. second"


# -- the engine's words survive the dedupe (card-239 interaction) ----------
def test_a_clipped_start_takes_the_words_before_it():
    words = [asr.Word(0.0, 1.0, " one"), asr.Word(1.0, 2.0, " two")]

    assert [word.text for word in asr._words_from(words, 1.0)] == [" two"]
    assert asr._words_from(words, 2.0) is None
    assert asr._words_from(None, 0.0) is None


# -- the file job path -----------------------------------------------------
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


class _EngineWord:
    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word
        self.probability = 0.95


class _Row:
    """One engine window with words; the class stands in for a segment."""

    seek = 0
    avg_logprob = -0.2
    compression_ratio = 1.1
    no_speech_prob = 0.0
    temperature = 0.0

    def __init__(self, start, end, text, words):
        self.start = start
        self.end = end
        self.text = text
        self.words = words


class _SentenceModel:
    """One window, two sentences with a pause between them."""

    def __init__(self):
        self.calls = []

    def transcribe(self, _audio, **kwargs):
        self.calls.append(dict(kwargs))
        row = _Row(0.2, 2.8, " first one. Second two.",
                   [_EngineWord(0.2, 0.6, " first"),
                    _EngineWord(0.6, 1.0, " one."),
                    _EngineWord(1.8, 2.2, " second"),
                    _EngineWord(2.2, 2.8, " two.")])
        return iter([row]), _Info()


def _wait(job, timeout=15.0):
    import file_jobs

    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in file_jobs.TERMINAL:
            return job.state
        time.sleep(0.02)
    return job.state


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_a_file_job_writes_one_cue_per_sentence(tmp_path):
    import file_jobs

    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, _Log())
    engine.model = _SentenceModel()
    queue = file_jobs.FileJobs(
        {"language": "ru", "model": "fake", "file_max_mb": 0,
         "job_keep_s": 3600, "file_roots": [], "file_word_timestamps": True},
        engine, _Log())
    try:
        job = queue.submit(str(TEST_WAV), formats=["srt"], out=str(tmp_path),
                           trusted=True)
        assert _wait(job) == "done"
    finally:
        queue.shutdown()

    written = (tmp_path / "test_ru.srt").read_text(encoding="utf-8")
    assert written == ("1\n00:00:00,200 --> 00:00:01,000\nfirst one.\n"
                       "\n"
                       "2\n00:00:01,800 --> 00:00:02,800\nsecond two.\n")
    assert job.segments[0].words is not None
    assert len(job.segments) == 1  # one engine window, two cues
