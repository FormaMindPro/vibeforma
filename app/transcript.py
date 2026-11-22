"""Transcript segments and their text formats: TXT, JSON, SRT and VTT.

One shape for every entrance: dictation inserts text, the file mode writes
segments. Timestamps stay non-negative and monotonic, empty segments are
dropped, and a line break inside a cue never breaks the block.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

FORMATS = ("txt", "json", "srt", "vtt")


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] | None = None


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
        out.append(Segment(start=start, end=end, text=text, words=seg.words))
    return out


def to_txt(segments: list[Segment]) -> str:
    """Paragraphs, one empty line between segments."""
    blocks = [seg.text.strip() for seg in _clean(segments)]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_json(segments: list[Segment], meta: dict[str, Any] | None = None) -> str:
    """Machine-readable transcript: full text, facts and segments."""
    meta = dict(meta or {})
    rows = []
    for seg in _clean(segments):
        row: dict[str, Any] = {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text,
        }
        if seg.words:
            row["words"] = [
                {"start": round(w.start, 3), "end": round(w.end, 3),
                 "text": w.text}
                for w in seg.words
            ]
        rows.append(row)
    payload = {
        "text": " ".join(row["text"] for row in rows),
        "language": str(meta.get("language") or ""),
        "model": str(meta.get("model") or ""),
        "duration": round(float(meta.get("duration") or 0.0), 3),
        "segments": rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
    """SubRip: numbered cues, comma before milliseconds."""
    rows = []
    for number, seg in enumerate(_clean(segments), start=1):
        rows.append(f"{number}\n"
                    f"{_clock(seg.start, ',')} --> {_clock(seg.end, ',')}\n"
                    f"{_cue_text(seg.text)}\n")
    return "\n".join(rows)


def to_vtt(segments: list[Segment]) -> str:
    """WebVTT: a header, cues with a dot before milliseconds."""
    rows = ["WEBVTT\n"]
    for seg in _clean(segments):
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
