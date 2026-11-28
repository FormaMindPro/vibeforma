"""Transcript segments and their text formats: TXT, JSON, SRT and VTT.

One shape for every entrance: dictation inserts text, the file mode writes
segments. Timestamps stay non-negative and monotonic, empty segments are
dropped, and a line break inside a cue never breaks the block.

SRT and VTT cut their cues by sentence where the engine left word
timestamps: a terminator or a pause closes a cue, and a window boundary
never does (see ``sentence_captions``).

The engine's confidence readings travel on the segment and its words; the
JSON format publishes them and ``needs_review`` turns them into the one
decision the dictation path makes: paste, or wait in the panel draft.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

FORMATS = ("txt", "json", "srt", "vtt")

# Confidence readings: the engine's own verdict on a window. A segment below
# one of these floors, or decoded above zero temperature, is unsure; a word
# below the word floor is marked in the panel draft.
LOGPROB_FLOOR = -1.0           # faster-whisper's own logprob_threshold
NO_SPEECH_CEILING = 0.6        # above it the window is more silence than speech
COMPRESSION_CEILING = 2.4      # above it the window repeats itself (hallucination)
WORD_PROBABILITY_FLOOR = 0.6   # a word below it is highlighted as doubtful

# A subtitle cue is a sentence, not an engine window. With word timestamps the
# boundaries come from the words themselves: a terminator closes the cue, and
# so does a silence between two words of ``CAPTION_PAUSE_S`` or longer. The
# same gap rule glues words the engine split across its 30-second windows or
# across file blocks, so an uninterrupted sentence stays one cue. Without
# words nothing can be re-cut and the engine's segments stand as they did.
CAPTION_PAUSE_S = 0.6
_SENTENCE_END = ".!?\u2026"
_SENTENCE_CLOSERS = "\"'\u00bb\u201d)]}"
# Punctuation that attaches to the word before it, never opening a new one.
_NO_SPACE_BEFORE = ",.!?;:%)\u2026\u2014\u2013\u00bb\u201d"


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float | None = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] | None = None
    # Kept from the engine, never recomputed here: temperature > 0 means the
    # window's first decode failed and a fallback ran; avg_logprob,
    # no_speech_prob and compression_ratio are the checks that failed it.
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    no_speech_prob: float | None = None
    temperature: float | None = None


@dataclass
class Transcript:
    """Segments plus the facts the JSON format publishes."""

    segments: list[Segment] = field(default_factory=list)
    language: str = ""
    model: str = ""
    duration: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(seg.text.strip() for seg in self.segments
                        if seg.text.strip())


def _clean(segments: list[Segment]) -> list[Segment]:
    """Drop empty cues and keep the timeline non-negative and monotonic."""
    out: list[Segment] = []
    previous = 0.0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        start = max(previous, max(0.0, float(seg.start)))
        end = max(start, max(0.0, float(seg.end)))
        previous = start
        out.append(Segment(start=start, end=end, text=text, words=seg.words,
                           avg_logprob=seg.avg_logprob,
                           compression_ratio=seg.compression_ratio,
                           no_speech_prob=seg.no_speech_prob,
                           temperature=seg.temperature))
    return out


def words_text(words: list[Word]) -> str:
    """A cue's text from its words, punctuation attached as the speech has it.

    The engine's word text carries its own leading space and its punctuation;
    a word that starts a new one is separated, a mark that attaches to the
    word before it is not. Line breaks never reach a cue from here: a cue is
    one line of speech.
    """
    text = ""
    for word in words:
        piece = (word.text or "").replace("\r", " ").replace("\n", " ")
        if not piece.strip():
            continue
        if text and not piece[0].isspace() and piece[0] not in _NO_SPACE_BEFORE:
            text += " "
        text += piece
    return " ".join(text.split())


def _ends_sentence(word: Word) -> bool:
    """Whether the word carries the sentence's own terminator (or its quote)."""
    value = (word.text or "").strip().rstrip(_SENTENCE_CLOSERS)
    return bool(value) and value[-1] in _SENTENCE_END


def _reading(seg: Segment, start: float, end: float, text: str,
             words: list[Word] | None) -> Segment:
    """A cue with the engine's reading of the window its first word came from."""
    return Segment(start=start, end=end, text=text, words=words,
                   avg_logprob=seg.avg_logprob,
                   compression_ratio=seg.compression_ratio,
                   no_speech_prob=seg.no_speech_prob,
                   temperature=seg.temperature)


def sentence_captions(segments: list[Segment],
                      pause_s: float = CAPTION_PAUSE_S) -> list[Segment]:
    """Cues cut where the sentence ends, not where the engine's window does.

    Every word carries the time the engine aligned it to. The walk glues the
    words of consecutive segments into one cue while the silence between them
    stays below ``pause_s`` and the sentence has not ended, so neither the
    30-second engine window nor the 600-second block seam closes a cue on its
    own. A sentence terminator and a pause at or above ``pause_s`` both close
    it; the next word opens a new cue at its own time.

    A segment without words cannot be re-cut and stands as a cue of its own
    (a block whose alignment was retried carries none, card-239). When no
    segment carries words at all, the list comes back cleaned exactly as
    before: the path without word timestamps is unchanged.

    The confidence readings of a cue are the ones of the segment its first
    word came from. The engine's confidence is per window, and a cue never
    invents a reading of its own.
    """
    if not any(seg.words for seg in segments):
        return _clean(segments)
    pause = max(0.0, float(pause_s))
    cues: list[Segment] = []
    buffer: list[Word] = []
    owner: Segment | None = None
    previous_end = 0.0

    def close() -> None:
        nonlocal buffer, owner, previous_end
        if buffer and owner is not None:
            text = words_text(buffer)
            if text:
                start = max(previous_end, max(0.0, float(buffer[0].start)))
                end = max(start, max(0.0, max(float(word.end)
                                               for word in buffer)))
                cues.append(_reading(owner, start, end, text, list(buffer)))
                previous_end = end
        buffer = []
        owner = None

    for seg in segments:
        if not seg.words:
            close()
            text = (seg.text or "").strip()
            if text:
                start = max(previous_end, max(0.0, float(seg.start)))
                end = max(start, max(0.0, float(seg.end)))
                cues.append(_reading(seg, start, end, text, None))
                previous_end = end
            continue
        for word in seg.words:
            if not (word.text or "").strip():
                continue
            if buffer:
                gap = float(word.start) - float(buffer[-1].end)
                if gap >= pause or _ends_sentence(buffer[-1]):
                    close()
            if not buffer:
                owner = seg
            buffer.append(word)
    close()
    return cues


def to_txt(segments: list[Segment]) -> str:
    """Paragraphs, one empty line between segments."""
    blocks = [seg.text.strip() for seg in _clean(segments)]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _word_row(word: Word) -> dict[str, Any]:
    row: dict[str, Any] = {"start": round(word.start, 3),
                           "end": round(word.end, 3), "text": word.text}
    if word.probability is not None:
        row["probability"] = round(float(word.probability), 4)
    return row


def to_json(segments: list[Segment], meta: dict[str, Any] | None = None) -> str:
    """Machine-readable transcript in the OpenAI verbose_json shape.

    Segments carry the engine's confidence readings, per-segment words keep
    the ``text`` key of the file format, and the top-level ``words`` array
    names every word of the file with ``word``/``start``/``end`` and the
    engine's ``probability`` — the shape an OpenAI client reads.
    """
    meta = dict(meta or {})
    cleaned = _clean(segments)
    rows = []
    all_words: list[dict[str, Any]] = []
    for seg in cleaned:
        row: dict[str, Any] = {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text,
        }
        for name in ("avg_logprob", "compression_ratio", "no_speech_prob",
                     "temperature"):
            value = getattr(seg, name)
            if value is not None:
                row[name] = round(float(value), 4)
        if seg.words:
            row["words"] = [
                _word_row(w) for w in seg.words
            ]
            for word in seg.words:
                item: dict[str, Any] = {
                    "word": word.text,
                    "start": round(word.start, 3),
                    "end": round(word.end, 3),
                }
                if word.probability is not None:
                    item["probability"] = round(float(word.probability), 4)
                all_words.append(item)
        rows.append(row)
    payload = {
        "text": " ".join(row["text"] for row in rows),
        "language": str(meta.get("language") or ""),
        "model": str(meta.get("model") or ""),
        "duration": round(float(meta.get("duration") or 0.0), 3),
        "segments": rows,
        "words": all_words,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def needs_review(segments: list[Segment]) -> bool:
    """True when the engine's own readings say the phrase is unsure.

    ``temperature > 0`` is checked first and on its own: a window decoded
    above zero already failed its first pass's logprob or compression check,
    so a retry's ``avg_logprob`` looking fine never clears it.
    """
    for seg in segments:
        if seg.temperature is not None and seg.temperature > 0.0:
            return True
        if (seg.avg_logprob is not None
                and seg.avg_logprob < LOGPROB_FLOOR):
            return True
        if (seg.no_speech_prob is not None
                and seg.no_speech_prob > NO_SPEECH_CEILING):
            return True
        if (seg.compression_ratio is not None
                and seg.compression_ratio > COMPRESSION_CEILING):
            return True
    return False


def uncertain_words(segments: list[Segment],
                    floor: float = WORD_PROBABILITY_FLOOR) -> list[str]:
    """The words the engine is unsure about, in order, each one once."""
    out: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        for word in seg.words or []:
            probability = word.probability
            if probability is None or probability >= floor:
                continue
            text = (word.text or "").strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
    return out


def uncertain_spans(text: str, words: list[str]) -> list[tuple[int, int]]:
    """Character spans of these words inside ``text``, case-insensitive.

    A word the dictionary or the text rules replaced is no longer there and
    yields no span: the draft is still a draft, it just has nothing to mark.
    """
    spans: list[tuple[int, int]] = []
    lowered = text.lower()
    for word in words:
        needle = (word or "").strip().lower()
        if not needle:
            continue
        start = lowered.find(needle)
        while start != -1:
            end = start + len(needle)
            if (start == 0 or not lowered[start - 1].isalnum()) and (
                    end == len(lowered) or not lowered[end].isalnum()):
                spans.append((start, end))
            start = lowered.find(needle, end)
    spans.sort()
    return spans


def _clock(seconds: float, mark: str) -> str:
    """HH:MM:SS<mark>mmm; negative input is clamped to zero.

    One integer millisecond total is rounded first, then split with divmod:
    no component is rounded on its own, so 59.9995 becomes 00:01:00,000 and
    the millisecond field is always 000..999, never ,1000.
    """
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{mark}{millis:03d}"


def _cue_text(text: str) -> str:
    """A break inside a cue stays inside the block, never starts a new one."""
    return text.strip().replace("\r\n", "\n").replace("\r", "\n")


def to_srt(segments: list[Segment]) -> str:
    """SubRip: numbered cues, comma before milliseconds.

    Cues follow the sentences where the engine left word timestamps
    (``sentence_captions``); without them the engine's segments stand, one
    cue per window, exactly as before.
    """
    rows = []
    for number, seg in enumerate(sentence_captions(segments), start=1):
        rows.append(f"{number}\n"
                    f"{_clock(seg.start, ',')} --> {_clock(seg.end, ',')}\n"
                    f"{_cue_text(seg.text)}\n")
    return "\n".join(rows)


def to_vtt(segments: list[Segment]) -> str:
    """WebVTT: a header, cues with a dot before milliseconds.

    The cue cut is the SRT one: a sentence closes its own cue.
    """
    rows = ["WEBVTT\n"]
    for seg in sentence_captions(segments):
        rows.append(f"{_clock(seg.start, '.')} --> {_clock(seg.end, '.')}\n"
                    f"{_cue_text(seg.text)}\n")
    return "\n".join(rows)


def render(segments: list[Segment], fmt: str,
           meta: dict[str, Any] | None = None) -> str:
    """One dispatcher for the four formats; an unknown name is an error."""
    name = (fmt or "").strip().lower()
    if name == "txt":
        return to_txt(segments)
    if name == "json":
        return to_json(segments, meta)
    if name == "srt":
        return to_srt(segments)
    if name == "vtt":
        return to_vtt(segments)
    raise ValueError(f"unknown format: {fmt}")
