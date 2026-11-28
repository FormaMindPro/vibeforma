"""Hallucination guard: thresholds and a stop-list against invented text.

Card-240 (F3, closes B4): silence and music must not turn into text. The
guard has two entrances -- the engine's own options (silence skip, ratio) and
a post-filter that cuts a segment only when ``no_speech_prob`` says the
window is non-speech and the text is a known false phrase or a loop. The F0
reference is the line the guard must not cross: clean Russian speech carries
ratio 1.476 and ``no_speech_prob`` 0.02, both untouched.

The repository is ASCII-only outside its localization resources, so the
Russian phrases are ``\\u`` escapes.
"""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first
import hallucination  # noqa: E402 - imported after numpy
from transcript import Segment  # noqa: E402

# "subscribe to the channel", as the card names it.
SUBSCRIBE = (" \u041f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439"
             "\u0442\u0435\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b!")
SUBSCRIBE_NORM = ("\u043f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439"
                  "\u0442\u0435\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b")
# "thanks for watching"
THANKS = (" \u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 "
          "\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440")


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
    duration = 4.0


class _Row:
    """faster-whisper's segment shape, readings included."""

    def __init__(self, segment=None, **extra):
        self.seek = 0
        self.start = getattr(segment, "start", 0.0)
        self.end = getattr(segment, "end", 0.0)
        self.text = getattr(segment, "text", "")
        self.words = getattr(segment, "words", None)
        for name in ("avg_logprob", "compression_ratio", "no_speech_prob",
                     "temperature"):
            setattr(self, name, getattr(segment, name, None))
        for key, value in extra.items():
            setattr(self, key, value)


class _Model:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))

        def _rows():
            for row in self.rows:
                yield _Row(row)

        return _rows(), _Info()


def _engine(rows, language="ru"):
    engine = asr.ASR({"language": language, "model": "fake", "hotwords": "",
                      "dictionary": []}, _Log())
    engine.model = _Model(rows)
    return engine


def _audio(seconds=3.0):
    return np.zeros((int(seconds * 16000),), dtype=np.float32)


def _spoken(text, **readings):
    return Segment(0.0, 1.0, text, **readings)


# -- the post-filter alone ----------------------------------------------
def test_a_stop_phrase_on_silence_is_dropped():
    segment = _spoken(SUBSCRIBE, no_speech_prob=0.9, compression_ratio=1.1)

    verdict = hallucination.judge(segment)

    assert verdict is not None
    assert verdict[0] == hallucination.DROP
    assert SUBSCRIBE_NORM in verdict[1]


def test_the_same_stop_phrase_in_real_speech_is_kept():
    segment = _spoken(SUBSCRIBE, no_speech_prob=0.05, compression_ratio=1.1)

    assert hallucination.judge(segment) is None


@pytest.mark.parametrize("text", [
    pytest.param(" Thanks for watching!", id="en-thanks"),
    pytest.param(" Subtitles by the Amara.org community", id="en-amara"),
    pytest.param(" Please subscribe to my channel", id="en-subscribe"),
    pytest.param(" \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
                 "\u0441\u0434\u0435\u043b\u0430\u043b DimaTorzok",
                 id="ru-subtitles-made"),
    pytest.param(" \u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 "
                 "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
                 "\u0410\u043b\u0435\u043a\u0441\u0435\u044e "
                 "\u0414\u0443\u0431\u0440\u043e\u0432\u0441\u043a\u043e\u043c\u0443",
                 id="ru-subtitles-thanks"),
    pytest.param(" \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
                 "\u043f\u043e\u0434\u043e\u0433\u043d\u0430\u043b ...",
                 id="ru-subtitles-fitted"),
    pytest.param(" \u0420\u0435\u0434\u0430\u043a\u0442\u043e\u0440 "
                 "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u043e\u0432 "
                 "\u0410. \u0421\u0438\u043d\u0435\u0446\u043a\u0430\u044f, "
                 "\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043e\u0440 "
                 "\u0410. \u0415\u0433\u043e\u0440\u043e\u0432\u0430",
                 id="ru-credits"),
])
def test_the_known_false_phrases_are_stop_phrases(text):
    segment = _spoken(text, no_speech_prob=0.9, compression_ratio=1.1)

    verdict = hallucination.judge(segment)

    assert verdict is not None and verdict[0] == hallucination.DROP


def test_ordinary_text_with_high_no_speech_is_kept():
    """The guard must not turn every quiet window into a cut."""
    segment = _spoken(" unclear phrase", no_speech_prob=0.7,
                      compression_ratio=1.2, temperature=0.0)

    assert hallucination.judge(segment) is None


def test_the_f0_reference_readings_are_untouched():
    """The F0 fixture's measured readings: ratio 1.476, no_speech 0.02.

    The text is an English stand-in: the guard reads the ratio the engine
    reported, never recomputing it from the text, and the phrase list is not
    language-gated.
    """
    segment = _spoken(
        " The quick brown fox jumps over the lazy dog and then asks whether "
        "the dictation works on a Tuesday afternoon.",
        no_speech_prob=0.019, compression_ratio=1.476, avg_logprob=-0.137,
        temperature=0.0)

    assert hallucination.judge(segment) is None


def test_a_loop_on_non_speech_is_dropped():
    segment = _spoken(" music plays music plays music plays",
                      no_speech_prob=0.9, compression_ratio=3.2)

    verdict = hallucination.judge(segment)

    assert verdict is not None and verdict[0] == hallucination.DROP
    assert "repetition on non-speech" in verdict[1]


def test_a_loop_the_engine_reads_as_speech_is_flagged_not_cut():
    """A real repeated phrase must never be cut on the ratio alone."""
    segment = _spoken(" music plays music plays music plays",
                      no_speech_prob=0.05, compression_ratio=3.2)

    verdict = hallucination.judge(segment)

    assert verdict is not None and verdict[0] == hallucination.FLAG
    assert "repetition loop" in verdict[1]


def test_a_repeated_short_answer_is_not_a_loop():
    segment = _spoken(" yes, yes, yes.", no_speech_prob=0.05,
                      compression_ratio=0.9)

    assert hallucination.judge(segment) is None


def test_normalization_ignores_case_and_punctuation():
    shout = ("\u041f\u041e\u0414\u041f\u0418\u0421\u042b\u0412\u0410\u0419"
             "\u0422\u0415\u0421\u042c, \u041d\u0410 \u041a\u0410\u041d\u0410"
             "\u041b!!!")
    assert hallucination.stop_phrase(shout) == SUBSCRIBE_NORM

    made = ("\u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u2014 "
            "\u0441\u0434\u0435\u043b\u0430\u043b: DimaTorzok")
    assert hallucination.stop_phrase(made) == (
        "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441\u0434\u0435"
        "\u043b\u0430\u043b")


# -- the engine options -------------------------------------------------
def test_the_silence_guard_reaches_the_engine_with_word_timestamps():
    engine = _engine([_spoken(" hello")])

    engine.transcribe(_audio())

    _audio_arg, kwargs = engine.model.calls[-1]
    assert kwargs["word_timestamps"] is True
    assert kwargs["hallucination_silence_threshold"] == (
        hallucination.HALLUCINATION_SILENCE_S)


def test_the_ratio_threshold_goes_to_the_language_it_was_calibrated_on():
    russian = _engine([_spoken(" hello")], language="ru")
    russian.transcribe(_audio())

    _audio_arg, kwargs = russian.model.calls[-1]
    assert "compression_ratio_threshold" not in kwargs

    english = _engine([_spoken(" hello")], language="en")
    english.transcribe(_audio())

    _audio_arg, kwargs = english.model.calls[-1]
    assert kwargs["compression_ratio_threshold"] == (
        hallucination.COMPRESSION_RATIO_THRESHOLD)


# -- the filter on both entrances ---------------------------------------
def test_a_file_drops_invented_text_and_keeps_the_speech():
    engine = _engine([
        Segment(0.0, 1.0, " first phrase"),
        Segment(1.0, 2.0, SUBSCRIBE, no_speech_prob=0.91,
                compression_ratio=1.2),
        Segment(2.0, 3.0, " third phrase"),
    ])

    segments = list(engine.transcribe_array(_audio(), 3.0))

    assert [s.text for s in segments] == ["first phrase", "third phrase"]
    assert any("Dropped invented text" in line
               for _level, line in engine.log.lines)


def test_a_dictation_take_drops_invented_text_before_the_service_sees_it():
    engine = _engine([
        Segment(0.0, 1.0, " first phrase"),
        Segment(1.0, 2.0, THANKS, no_speech_prob=0.93,
                compression_ratio=1.1),
    ])

    text = engine.transcribe(_audio())

    assert text == "first phrase"
    assert [s.text for s in engine.last_segments] == [" first phrase"]


def test_a_partial_keeps_the_raw_preview():
    engine = _engine([
        Segment(0.0, 1.0, SUBSCRIBE, no_speech_prob=0.93,
                compression_ratio=1.1),
    ])

    text = engine.transcribe(_audio(1.0), partial=True)

    assert text == SUBSCRIBE.strip()


# -- the invented tail --------------------------------------------------
# " Subtitles made by DimaTorzok": the name after the phrase is part of the
# invented line and goes with it.
CREDIT = (" \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
          "\u0441\u0434\u0435\u043b\u0430\u043b DimaTorzok")
CREDIT_NORM = ("\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
               "\u0441\u0434\u0435\u043b\u0430\u043b")
# " Thanks for the subtitles Alexey Dubrovsky": the reported invented line.
THANKS_SUBS = (" \u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 "
               "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
               "\u0410\u043b\u0435\u043a\u0441\u0435\u044e "
               "\u0414\u0443\u0431\u0440\u043e\u0432\u0441\u043a\u043e\u043c\u0443")
THANKS_SUBS_NORM = ("\u0441\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 "
                    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b")
# " Hello", " subtitles made", " by DimaTorzok"
class _Word:
    """faster-whisper's word shape: the text rides in ``word``."""

    probability = None

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.word = text


CREDIT_WORDS = [
    _Word(0.4, 0.7, " \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b"),
    _Word(0.7, 1.0, " \u0441\u0434\u0435\u043b\u0430\u043b"),
    _Word(1.0, 1.4, " DimaTorzok"),
]
HELLO = " \u041f\u0440\u0438\u0432\u0435\u0442"


def _vad(monkeypatch, speeches, calls=None):
    """Replace the VAD: the real Silero model never loads in these tests."""

    def fake(audio, vad_options=None, sampling_rate=None):
        if calls is not None:
            calls.append(sampling_rate)
        return speeches

    monkeypatch.setattr(asr, "get_speech_timestamps", fake)


def test_a_trailing_stop_phrase_written_into_silence_is_cut(monkeypatch):
    engine = _engine([
        Segment(0.0, 0.4, HELLO, words=[_Word(0.0, 0.4, HELLO)]),
        Segment(0.4, 1.4, CREDIT, words=list(CREDIT_WORDS)),
    ])
    _vad(monkeypatch, [{"start": 0, "end": int(0.4 * 16000)}])

    text = engine.transcribe(_audio(2.0))

    assert text == HELLO.strip()
    assert [s.text for s in engine.last_segments] == [HELLO]
    warned = [line for level, line in engine.log.lines if level == "warning"]
    assert any(CREDIT_NORM in line for line in warned)
    assert all(HELLO.strip() not in line for line in warned)


def test_the_reported_subtitles_thanks_line_is_cut(monkeypatch):
    """The credits line with a name after it goes with the name."""
    words = [
        _Word(0.45, 0.50, " \u0421\u043f\u0430\u0441\u0438\u0431\u043e"),
        _Word(0.50, 0.53, " \u0437\u0430"),
        _Word(0.53, 0.60, " \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b"),
        _Word(0.60, 0.65, " \u0410\u043b\u0435\u043a\u0441\u0435\u044e"),
        _Word(0.65, 0.70, " \u0414\u0443\u0431\u0440\u043e\u0432\u0441\u043a\u043e\u043c\u0443"),
    ]
    engine = _engine([
        Segment(0.0, 0.4, HELLO, words=[_Word(0.0, 0.4, HELLO)]),
        Segment(0.4, 0.7, THANKS_SUBS, words=words),
    ])
    _vad(monkeypatch, [{"start": 0, "end": int(0.4 * 16000)}])

    text = engine.transcribe(_audio(2.0))

    assert text == HELLO.strip()
    assert [s.text for s in engine.last_segments] == [HELLO]
    warned = [line for level, line in engine.log.lines if level == "warning"]
    assert any(THANKS_SUBS_NORM in line for line in warned)


def test_a_trailing_stop_phrase_that_was_spoken_is_kept(monkeypatch):
    engine = _engine([Segment(0.0, 1.4, CREDIT, words=list(CREDIT_WORDS))])
    _vad(monkeypatch, [{"start": 0, "end": int(1.4 * 16000)}])

    text = engine.transcribe(_audio(2.0))

    assert text == CREDIT.strip()
    assert [s.text for s in engine.last_segments] == [CREDIT]


def test_a_stop_phrase_inside_the_take_is_not_a_tail(monkeypatch):
    """The tail runs to the end, so the speech after the phrase keeps it."""
    text_line = (" \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b "
                 "\u0441\u0434\u0435\u043b\u0430\u043b DimaTorzok "
                 "\u0438 \u043f\u043e\u0442\u043e\u043c \u043c\u044b "
                 "\u043f\u043e\u0448\u043b\u0438 \u0433\u0443\u043b\u044f\u0442\u044c")
    words = [
        _Word(0.0, 0.3, " \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b"),
        _Word(0.3, 0.6, " \u0441\u0434\u0435\u043b\u0430\u043b"),
        _Word(0.6, 0.9, " DimaTorzok"),
        _Word(1.5, 1.6, " \u0438"),
        _Word(1.6, 1.8, " \u043f\u043e\u0442\u043e\u043c"),
        _Word(1.8, 2.0, " \u043c\u044b"),
        _Word(2.0, 2.2, " \u043f\u043e\u0448\u043b\u0438"),
        _Word(2.2, 2.4, " \u0433\u0443\u043b\u044f\u0442\u044c"),
    ]
    engine = _engine([Segment(0.0, 2.4, text_line, words=words)])
    _vad(monkeypatch, [{"start": int(1.5 * 16000),
                        "end": int(2.4 * 16000)}])

    text = engine.transcribe(_audio(3.0))

    assert text == text_line.strip()
    assert [s.text for s in engine.last_segments] == [text_line]


def test_a_take_without_word_timestamps_and_with_speech_is_not_touched(monkeypatch):
    engine = _engine([Segment(0.0, 1.0, CREDIT)])
    calls = []
    _vad(monkeypatch, [{"start": 0, "end": 16000}], calls)

    text = engine.transcribe(_audio(2.0))

    assert text == CREDIT.strip()
    assert [s.text for s in engine.last_segments] == [CREDIT]
    assert calls == [16000]


def test_a_take_without_word_timestamps_in_silence_is_cut(monkeypatch):
    engine = _engine([Segment(0.0, 1.0, CREDIT)])
    calls = []
    _vad(monkeypatch, [], calls)

    text = engine.transcribe(_audio(2.0))

    assert text == ""
    assert engine.last_segments == []
    assert calls == [16000]


def test_a_tail_phrase_split_between_segments_is_cut(monkeypatch):
    first = (" \u041f\u0440\u0438\u0432\u0435\u0442 "
             "\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435")
    second = " \u0441\u043b\u0435\u0434\u0443\u0435\u0442..."
    engine = _engine([
        Segment(0.0, 0.5, first, words=[
            _Word(0.0, 0.4, " \u041f\u0440\u0438\u0432\u0435\u0442"),
            _Word(0.4, 0.5, " \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435"),
        ]),
        Segment(0.5, 0.6, second, words=[
            _Word(0.5, 0.6, " \u0441\u043b\u0435\u0434\u0443\u0435\u0442..."),
        ]),
    ])
    _vad(monkeypatch, [{"start": 0, "end": int(0.6 * 16000)}])

    text = engine.transcribe(_audio(2.0))

    assert text == "\u041f\u0440\u0438\u0432\u0435\u0442"
    assert [s.text for s in engine.last_segments] == [" \u041f\u0440\u0438\u0432\u0435\u0442"]


def test_a_compressed_tail_in_a_dense_take_is_cut(monkeypatch):
    filler = " word" * 16
    text = filler + (" \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435 "
                     "\u0441\u043b\u0435\u0434\u0443\u0435\u0442.")
    words = [_Word(i * 0.05, (i + 1) * 0.05, " word") for i in range(16)]
    words += [
        _Word(0.8, 1.1, " \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435"),
        _Word(1.1, 1.4, " \u0441\u043b\u0435\u0434\u0443\u0435\u0442."),
    ]
    engine = _engine([Segment(0.0, 1.4, text, words=words)])
    _vad(monkeypatch, [{"start": 0, "end": int(1.4 * 16000)}])

    result = engine.transcribe(_audio(2.0))

    assert "\u0441\u043b\u0435\u0434\u0443\u0435\u0442" not in result
    assert result.strip().startswith("word")


def test_a_long_take_cuts_only_the_phrase_not_the_words_before_it(monkeypatch):
    """The tail window must not shift the cut into the take's real words."""
    filler = " word" * 25
    text = filler + (" \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435 "
                     "\u0441\u043b\u0435\u0434\u0443\u0435\u0442.")
    words = [_Word(i * 0.045, (i + 1) * 0.045, " word") for i in range(25)]
    words += [
        _Word(1.125, 1.175, " \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435"),
        _Word(1.175, 1.225, " \u0441\u043b\u0435\u0434\u0443\u0435\u0442."),
    ]
    engine = _engine([Segment(0.0, 1.225, text, words=words)])
    _vad(monkeypatch, [{"start": 0, "end": int(1.225 * 16000)}])

    result = engine.transcribe(_audio(2.0))

    assert "\u0441\u043b\u0435\u0434\u0443\u0435\u0442" not in result
    assert result.split() == ["word"] * 25


def test_a_dense_text_only_tail_is_cut_even_with_speech(monkeypatch):
    text = (" word" * 16) + (" \u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435 "
                             "\u0441\u043b\u0435\u0434\u0443\u0435\u0442.")
    engine = _engine([Segment(0.0, 2.0, text)])
    _vad(monkeypatch, [{"start": 0, "end": 32000}])

    result = engine.transcribe(_audio(2.0))

    assert "\u0441\u043b\u0435\u0434\u0443\u0435\u0442" not in result
    assert result.strip().startswith("word")


def test_cutting_the_whole_take_leaves_no_empty_segment(monkeypatch):
    engine = _engine([Segment(0.0, 1.4, CREDIT + "!", words=[
        _Word(0.0, 0.4, " \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b"),
        _Word(0.4, 0.9, " \u0441\u0434\u0435\u043b\u0430\u043b"),
        _Word(0.9, 1.4, " DimaTorzok!"),
    ])])
    _vad(monkeypatch, [])

    text = engine.transcribe(_audio(2.0))

    assert text == ""
    assert engine.last_segments == []
    assert "!" not in text


def test_a_vad_failure_leaves_the_take_alone(monkeypatch):
    engine = _engine([Segment(0.0, 1.4, CREDIT, words=list(CREDIT_WORDS))])

    def boom(*args, **kwargs):
        raise RuntimeError("vad is down")

    monkeypatch.setattr(asr, "get_speech_timestamps", boom)

    text = engine.transcribe(_audio(2.0))

    assert text == CREDIT.strip()
    assert [s.text for s in engine.last_segments] == [CREDIT]
