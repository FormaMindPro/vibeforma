"""Hallucination guard: thresholds and a stop-list against invented text.

Whisper fills silence, music and room noise with text. The families are well
known: the subscribe line, the thanks-for-watching line, Amara.org subtitle
credits, and loops that repeat one phrase until the window ends. The guard
has two parts: the engine's own thresholds (the silence skip and the
compression ratio) and a post-filter that drops a segment only when the
engine itself says the window was more silence than speech
(``no_speech_prob``) and the text is a known false phrase or a loop.

The post-filter is deliberately narrow. A stop phrase inside real speech is
kept: a speaker can genuinely ask for subscribers, and only a window the
engine reads as non-speech turns that phrase into evidence. The same rule
keeps the F0 reference intact: clean Russian speech carries
``no_speech_prob`` near 0.02, so neither the stop-list nor the ratio touches
it.

``COMPRESSION_RATIO_THRESHOLD`` is OpenAI's 1.35 for large-v3/turbo. The
ratio is computed on UTF-8 bytes, one byte per Latin character and two for
Cyrillic. Measured on the F0 fixture (clean Russian speech, 12.5 s): ratio
1.476, ``no_speech_prob`` 0.02, and the engine's own threshold at 1.35 sends
that clean window through the whole six-step fallback ladder and reports
``temperature`` 1.0. ``ENGINE_RATIO_LANGUAGES`` therefore names the language
the value was calibrated on; every other language gets the same 1.35 in this
post-filter, where ``no_speech_prob`` corroborates it.

The repository is ASCII-only outside its localization resources (prohibition
19), so every Russian phrase below is a ``\\u`` escape; the comment beside
each one names it in English.
"""
from collections import Counter

# The engine's own threshold, in bytes (faster-whisper's get_compression_ratio).
COMPRESSION_RATIO_THRESHOLD = 1.35
# The language the 1.35 value is calibrated on; its text is one byte per
# character, so the ratio means what OpenAI measured. Other scripts inflate
# the ratio per character and are guarded by the post-filter instead.
ENGINE_RATIO_LANGUAGES = frozenset({"en"})
# The engine skips silence before a segment whose words look anomalous; the
# library needs word timestamps for it and ignores it otherwise. Two seconds
# is the value community write-ups use (the library's own default is off).
HALLUCINATION_SILENCE_S = 2.0
# Above it the window is more silence than speech (faster-whisper's default).
NO_SPEECH_CEILING = 0.6
# A loop repeats one word through the text; a real sentence does not.
LOOP_MIN_WORDS = 4
LOOP_WORD_SHARE = 0.5

DROP = "drop"
FLAG = "flag"

# Normalized: lower case, "e" folded from its accented form, punctuation and
# spacing collapsed. Every phrase is a substring test against a normalized
# segment; the list names the families of the report and the findings, both
# languages.
STOP_PHRASES = (
    # English
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe to my channel",
    "subscribe to the channel",
    "like and subscribe",
    "subtitles by",
    "subtitled by",
    "amara org",
    "transcribed by",
    "transcription by",
    # Russian: subscribe to the channel
    "\u043f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439\u0442\u0435\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b",
    # Russian: subscribe to my channel
    "\u043f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439\u0442\u0435\u0441\u044c \u043d\u0430 \u043c\u043e\u0439 \u043a\u0430\u043d\u0430\u043b",
    # Russian: do not forget to subscribe
    "\u043d\u0435 \u0437\u0430\u0431\u0443\u0434\u044c\u0442\u0435 \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f",
    # Russian: put a like
    "\u0441\u0442\u0430\u0432\u044c\u0442\u0435 \u043b\u0430\u0439\u043a",
    # Russian: put likes
    "\u0441\u0442\u0430\u0432\u044c\u0442\u0435 \u043b\u0430\u0439\u043a\u0438",
    # Russian: thanks for watching
    "\u0441\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
    # Russian: subtitles made by (m.)
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441\u0434\u0435\u043b\u0430\u043b",
    # Russian: subtitles made by (f.)
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441\u0434\u0435\u043b\u0430\u043b\u0430",
    # Russian: subtitles created by (m.)
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441\u043e\u0437\u0434\u0430\u043b",
    # Russian: subtitles created by (f.)
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441\u043e\u0437\u0434\u0430\u043b\u0430",
    # Russian: subtitles fitted by
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u043f\u043e\u0434\u043e\u0433\u043d\u0430\u043b",
    # Russian: subtitles prepared by
    "\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u043b",
    # Russian: subtitles editor
    "\u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440 \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u043e\u0432",
    # Russian: proofreader
    "\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043e\u0440",
    # Russian: to be continued
    "\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435 \u0441\u043b\u0435\u0434\u0443\u0435\u0442",
)

# The letters the normalization folds: "yo" is written as "e" so one phrase
# matches both spellings.
_FOLD = str.maketrans({"\u0451": "\u0435"})


def normalize(text: str) -> str:
    """Lower case, the accented "yo" folded, punctuation dropped, spacing collapsed."""
    lowered = str(text or "").lower().translate(_FOLD)
    cleaned = "".join(ch if ch.isalnum() else " " for ch in lowered)
    return " ".join(cleaned.split())


def stop_phrase(text: str) -> str | None:
    """The stop phrase the text contains, normalized, or None."""
    normalized = normalize(text)
    if not normalized:
        return None
    for phrase in STOP_PHRASES:
        if phrase in normalized:
            return phrase
    return None


def is_loop(text: str) -> bool:
    """True when one word takes half the text: the shape of a repetition."""
    words = normalize(text).split()
    if len(words) < LOOP_MIN_WORDS:
        return False
    return max(Counter(words).values()) / len(words) >= LOOP_WORD_SHARE


def judge(segment, *, no_speech_ceiling: float = NO_SPEECH_CEILING,
          ratio_threshold: float = COMPRESSION_RATIO_THRESHOLD,
          ) -> tuple[str, str] | None:
    """What the guard makes of one segment: ``(action, reason)`` or None.

    ``DROP`` is invented text: the engine reads the window as non-speech and
    the text is a stop phrase or a repetition loop. ``FLAG`` is a repetition
    loop the engine still reads as speech; it is kept and named in the log,
    because a real repeated phrase must never be cut on the ratio alone.
    """
    text = str(getattr(segment, "text", "") or "").strip()
    if not text:
        return None
    no_speech = getattr(segment, "no_speech_prob", None)
    ratio = getattr(segment, "compression_ratio", None)
    quiet = no_speech is not None and float(no_speech) >= no_speech_ceiling
    phrase = stop_phrase(text)
    if quiet and phrase is not None:
        return DROP, (f"stop phrase {phrase!r} on non-speech "
                      f"(no_speech_prob {float(no_speech):.2f})")
    over = ratio is not None and float(ratio) > ratio_threshold
    if quiet and over:
        return DROP, (f"repetition on non-speech (compression_ratio "
                      f"{float(ratio):.2f} > {ratio_threshold:g}, "
                      f"no_speech_prob {float(no_speech):.2f})")
    if over and is_loop(text):
        return FLAG, (f"repetition loop (compression_ratio {float(ratio):.2f} "
                      f"> {ratio_threshold:g}): {text[:60]!r}")
    return None
