#!/usr/bin/env python
"""WER/CER reference for the local ASR engine (card-236).

The corpus is ``tests/wer/manifest.json``: short ru+en recordings with the
text that was actually spoken, plus noise, music and silence clips where no
text is expected. The same tool measures the existing ``test_ru.wav`` as the
base line.

Modes (run from the product root):

    .venv\\Scripts\\python.exe tools/wer_cer.py --self-test
    .venv\\Scripts\\python.exe tools/wer_cer.py --transcribe
    .venv\\Scripts\\python.exe tools/wer_cer.py --hyp-dir out\\hyp
    .venv\\Scripts\\python.exe tools/wer_cer.py --baseline

``--self-test`` checks the normalisation, the arithmetic and the manifest
without recordings or a model, so CI stays offline. ``--transcribe`` runs the
product ``ASR`` (the same decode path and settings as file mode) and writes
the report; ``--hyp-dir`` reads one ``<stem>.txt`` per recording instead.
``--baseline`` measures ``test_ru.wav`` and prints the actual transcript.

Normalisation (both reference and hypothesis, per phrase language):

* lower case, ``yo`` folded to ``ye`` (Russian);
* every non-alphanumeric character becomes a space;
* a standalone integer 0-999 becomes spoken words (ru and en), so ``12`` and
  ``dvenadtsat`` compare equal; digits glued to letters (``v3``) stay;
* WER is word-level Levenshtein over the normalised text; CER is character
  level without spaces. The ratio is errors / reference length; an empty
  reference with a non-empty hypothesis scores 1.0, which is the
  noise/music/silence false-positive case.

Exit codes: 0 report complete, 2 recordings missing or unreadable, 1 the
manifest or an argument is wrong.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
DEFAULT_MANIFEST = ROOT / "tests" / "wer" / "manifest.json"
PHRASE_COUNT = 24
SPEECH = "speech"
NON_SPEECH_KINDS = ("noise", "music", "silence")
REQUIRED_TAGS = ("names", "numbers", "terms", "noise", "music", "silence")
ALL_TAGS = ("baseline", *REQUIRED_TAGS)
SPEECH_LANGS = ("ru", "en")

_RU_ONES = (
    "\u043d\u043e\u043b\u044c", "\u043e\u0434\u0438\u043d", "\u0434\u0432\u0430",
    "\u0442\u0440\u0438", "\u0447\u0435\u0442\u044b\u0440\u0435", "\u043f\u044f\u0442\u044c",
    "\u0448\u0435\u0441\u0442\u044c", "\u0441\u0435\u043c\u044c", "\u0432\u043e\u0441\u0435\u043c\u044c",
    "\u0434\u0435\u0432\u044f\u0442\u044c", "\u0434\u0435\u0441\u044f\u0442\u044c",
    "\u043e\u0434\u0438\u043d\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0434\u0432\u0435\u043d\u0430\u0434\u0446\u0430\u0442\u044c", "\u0442\u0440\u0438\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0447\u0435\u0442\u044b\u0440\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u043f\u044f\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0448\u0435\u0441\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0441\u0435\u043c\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0432\u043e\u0441\u0435\u043c\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0434\u0435\u0432\u044f\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
)
_RU_TENS = (
    "", "", "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c", "\u0442\u0440\u0438\u0434\u0446\u0430\u0442\u044c",
    "\u0441\u043e\u0440\u043e\u043a", "\u043f\u044f\u0442\u044c\u0434\u0435\u0441\u044f\u0442",
    "\u0448\u0435\u0441\u0442\u044c\u0434\u0435\u0441\u044f\u0442", "\u0441\u0435\u043c\u044c\u0434\u0435\u0441\u044f\u0442",
    "\u0432\u043e\u0441\u0435\u043c\u044c\u0434\u0435\u0441\u044f\u0442", "\u0434\u0435\u0432\u044f\u043d\u043e\u0441\u0442\u043e",
)
_RU_HUNDREDS = (
    "", "\u0441\u0442\u043e", "\u0434\u0432\u0435\u0441\u0442\u0438", "\u0442\u0440\u0438\u0441\u0442\u0430",
    "\u0447\u0435\u0442\u044b\u0440\u0435\u0441\u0442\u0430", "\u043f\u044f\u0442\u044c\u0441\u043e\u0442",
    "\u0448\u0435\u0441\u0442\u044c\u0441\u043e\u0442", "\u0441\u0435\u043c\u044c\u0441\u043e\u0442",
    "\u0432\u043e\u0441\u0435\u043c\u044c\u0441\u043e\u0442", "\u0434\u0435\u0432\u044f\u0442\u044c\u0441\u043e\u0442",
)

_EN_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_EN_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)

_DIGITS = re.compile(r"(?<!\w)\d+(?!\w)")


class ManifestError(ValueError):
    """The manifest is not the expected JSON object."""


@dataclass(frozen=True)
class Phrase:
    """One manifest entry: a recording, its language and the spoken text."""

    id: str
    file: str
    language: str
    kind: str
    tags: tuple[str, ...]
    text: str
    instruction: str = ""


@dataclass
class Measure:
    """One recording compared with its reference."""

    phrase: Phrase
    status: str                     # measured | missing | error
    hypothesis: str = ""
    error: str = ""
    ref_words: int = 0
    hyp_words: int = 0
    subs: int = 0
    dels: int = 0
    ins: int = 0
    wer: float = 0.0
    ref_chars: int = 0
    cer_errors: int = 0
    cer: float = 0.0
    false_positive: bool = False


def number_words(value: int, language: str) -> str:
    """Spoken form of 0-999; other values stay as digits in ``normalize``."""
    if language == "ru":
        return _ru_words(value)
    if language == "en":
        return _en_words(value)
    return str(value)


def _en_words(value: int) -> str:
    if value == 0:
        return _EN_ONES[0]
    parts: list[str] = []
    hundreds, rest = divmod(value, 100)
    if hundreds:
        parts.extend((_EN_ONES[hundreds], "hundred"))
    if rest:
        if rest < 20:
            parts.append(_EN_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            parts.append(_EN_TENS[tens])
            if ones:
                parts.append(_EN_ONES[ones])
    return " ".join(parts)


def _ru_words(value: int) -> str:
    if value == 0:
        return _RU_ONES[0]
    parts: list[str] = []
    hundreds, rest = divmod(value, 100)
    if hundreds:
        parts.append(_RU_HUNDREDS[hundreds])
    if rest:
        if rest < 20:
            parts.append(_RU_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            parts.append(_RU_TENS[tens])
            if ones:
                parts.append(_RU_ONES[ones])
    return " ".join(parts)


def normalize(text: str, language: str = "en") -> str:
    """Lower case, punctuation to spaces, standalone digits as spoken words."""
    lowered = (text or "").lower().replace("\u0451", "\u0435")
    spaced = "".join(char if char.isalnum() else " " for char in lowered)

    def _replace(match: re.Match[str]) -> str:
        value = int(match.group(0))
        if 0 <= value <= 999:
            return " " + number_words(value, language) + " "
        return " " + match.group(0) + " "

    return " ".join(_DIGITS.sub(_replace, spaced).split())


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> tuple[int, int, int, int]:
    """Levenshtein distance and substitutions, deletions, insertions."""
    rows = len(reference) + 1
    cols = len(hypothesis) + 1
    costs = [[0] * cols for _ in range(rows)]
    moves: list[list[str]] = [[""] * cols for _ in range(rows)]
    for i in range(1, rows):
        costs[i][0] = i
        moves[i][0] = "d"
    for j in range(1, cols):
        costs[0][j] = j
        moves[0][j] = "i"
    for i in range(1, rows):
        for j in range(1, cols):
            if reference[i - 1] == hypothesis[j - 1]:
                costs[i][j] = costs[i - 1][j - 1]
                moves[i][j] = "m"
                continue
            substituted = costs[i - 1][j - 1] + 1
            deleted = costs[i - 1][j] + 1
            inserted = costs[i][j - 1] + 1
            best = min(substituted, deleted, inserted)
            costs[i][j] = best
            if best == substituted:
                moves[i][j] = "s"
            elif best == deleted:
                moves[i][j] = "d"
            else:
                moves[i][j] = "i"
    substitutions = deletions = insertions = 0
    i, j = len(reference), len(hypothesis)
    while i or j:
        move = moves[i][j]
        if move == "m":
            i -= 1
            j -= 1
        elif move == "s":
            substitutions += 1
            i -= 1
            j -= 1
        elif move == "d":
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    return costs[-1][-1], substitutions, deletions, insertions


def ratio(errors: int, length: int) -> float:
    """Error rate; an empty reference with output is a full error."""
    if length > 0:
        return errors / length
    return 0.0 if errors == 0 else 1.0


def measure(phrase: Phrase, hypothesis: str) -> Measure:
    """Compare one recording's text with its reference."""
    reference = normalize(phrase.text, phrase.language or "en")
    normalized = normalize(hypothesis, phrase.language or "en")
    ref_tokens = reference.split()
    hyp_tokens = normalized.split()
    ref_compact = reference.replace(" ", "")
    hyp_compact = normalized.replace(" ", "")
    if phrase.kind != SPEECH:
        distance, _subs, _dels, _ins = edit_distance(ref_compact, hyp_compact)
        false_positive = bool(hyp_tokens)
        return Measure(
            phrase=phrase,
            status="measured",
            hypothesis=hypothesis,
            hyp_words=len(hyp_tokens),
            wer=ratio(len(hyp_tokens), len(ref_tokens)),
            ref_chars=len(ref_compact),
            cer_errors=distance,
            cer=ratio(distance, len(ref_compact)),
            false_positive=false_positive,
        )
    distance, subs, dels, ins = edit_distance(ref_tokens, hyp_tokens)
    cer_distance, _cer_subs, _cer_dels, _cer_ins = edit_distance(ref_compact, hyp_compact)
    return Measure(
        phrase=phrase,
        status="measured",
        hypothesis=hypothesis,
        ref_words=len(ref_tokens),
        hyp_words=len(hyp_tokens),
        subs=subs,
        dels=dels,
        ins=ins,
        wer=ratio(distance, len(ref_tokens)),
        ref_chars=len(ref_compact),
        cer_errors=cer_distance,
        cer=ratio(cer_distance, len(ref_compact)),
    )


def load_manifest(path: Path) -> dict:
    """Read the JSON manifest; a broken file is a ManifestError."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"manifest: cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest: the root must be an object")
    return payload


def parse_phrases(payload: dict) -> list[Phrase]:
    """Phrases from the manifest, or ManifestError with the first bad entry."""
    raw = payload.get("phrases")
    if not isinstance(raw, list):
        raise ManifestError("manifest: 'phrases' must be a list")
    phrases: list[Phrase] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ManifestError(f"phrases[{index}]: expected an object")
        for key in ("id", "file", "language", "kind", "text"):
            if key not in item:
                raise ManifestError(f"phrases[{index}]: missing '{key}'")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ManifestError(f"phrases[{index}]: 'tags' must be a list of strings")
        phrases.append(Phrase(
            id=str(item["id"]),
            file=str(item["file"]),
            language=str(item["language"]),
            kind=str(item["kind"]),
            tags=tuple(tags),
            text=str(item["text"]),
            instruction=str(item.get("instruction", "")),
        ))
    return phrases


def parse_baseline(payload: dict) -> dict:
    """The test_ru.wav baseline block."""
    baseline = payload.get("baseline")
    if not isinstance(baseline, dict):
        raise ManifestError("manifest: missing 'baseline' object")
    for key in ("file", "language", "text"):
        if key not in baseline:
            raise ManifestError(f"baseline: missing '{key}'")
    return baseline


def validate_manifest(payload: dict) -> list[str]:
    """Every schema problem, empty when the manifest is complete."""
    problems: list[str] = []
    try:
        phrases = parse_phrases(payload)
    except ManifestError as exc:
        return [str(exc)]
    if len(phrases) != PHRASE_COUNT:
        problems.append(f"phrases: {len(phrases)} entries, expected {PHRASE_COUNT}")
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    covered: set[str] = set()
    for phrase in phrases:
        address = f"phrases[{phrase.id}]"
        if phrase.id in seen_ids:
            problems.append(f"{address}: duplicate id")
        seen_ids.add(phrase.id)
        if phrase.file in seen_files:
            problems.append(f"{address}: duplicate file {phrase.file}")
        seen_files.add(phrase.file)
        if Path(phrase.file).suffix.lower() != ".wav" or Path(phrase.file).name != phrase.file:
            problems.append(f"{address}: file must be a bare .wav name, got {phrase.file}")
        if phrase.kind not in (SPEECH, *NON_SPEECH_KINDS):
            problems.append(f"{address}: unknown kind {phrase.kind}")
        if phrase.kind == SPEECH:
            if phrase.language not in SPEECH_LANGS:
                problems.append(f"{address}: speech language must be ru or en")
            if not phrase.text.strip():
                problems.append(f"{address}: speech phrase needs text")
        else:
            if phrase.language:
                problems.append(f"{address}: {phrase.kind} must have an empty language")
            if phrase.text.strip():
                problems.append(f"{address}: {phrase.kind} must not expect text")
        for tag in phrase.tags:
            if tag not in ALL_TAGS:
                problems.append(f"{address}: unknown tag {tag}")
            covered.add(tag)
    for tag in REQUIRED_TAGS:
        if tag not in covered:
            problems.append(f"phrases: no entry carries the '{tag}' tag")
    try:
        baseline = parse_baseline(payload)
    except ManifestError as exc:
        problems.append(str(exc))
    else:
        if not str(baseline.get("file", "")).strip():
            problems.append("baseline: empty file")
        if baseline.get("language") not in SPEECH_LANGS:
            problems.append("baseline: language must be ru or en")
        if not str(baseline.get("text", "")).strip():
            problems.append("baseline: empty text")
    return problems


def _read_hypothesis(directory: Path, phrase: Phrase) -> str | None:
    for name in (Path(phrase.file).stem + ".txt", phrase.id + ".txt"):
        path = directory / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def _load_engine(model: str, device: str, compute_type: str):
    app_path = str(APP_DIR)
    if app_path not in sys.path:
        sys.path.insert(0, app_path)
    import asr

    logger = logging.getLogger("wer-cer")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    cfg = {
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "language": "en",
        "hotwords": "",
        "dictionary": [],
        "file_batched": False,
        "file_batch_size": 1,
    }
    engine = asr.ASR(cfg, logger)
    engine.load()
    return engine


def _transcribe(engine, path: Path, language: str) -> str:
    segments = engine.transcribe_file(str(path), language=language or "en", batched=False)
    return " ".join(
        (segment.text or "").strip()
        for segment in segments
        if (segment.text or "").strip()
    )


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_report(measures: list[Measure], manifest_path: Path, engine_note: str = "") -> str:
    """Per-file and total WER/CER; non-speech clips report false positives."""
    lines = ["WER/CER reference", f"manifest: {manifest_path}"]
    if engine_note:
        lines.append(f"engine: {engine_note}")
    lines.append("")
    header = (
        f"{'id':<11} {'lang':<4} {'kind':<9} {'refw':>5} {'hypw':>5} "
        f"{'S':>3} {'D':>3} {'I':>3} {'WER':>8} {'CER':>8}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for item in measures:
        phrase = item.phrase
        lang = phrase.language or "-"
        if item.status == "missing":
            lines.append(f"{phrase.id:<11} {lang:<4} {phrase.kind:<9} MISSING {phrase.file}")
            continue
        if item.status == "error":
            lines.append(f"{phrase.id:<11} {lang:<4} {phrase.kind:<9} ERROR {item.error}")
            continue
        if phrase.kind != SPEECH:
            state = "FALSE POSITIVE" if item.false_positive else "no speech"
            detail = f"{item.hyp_words} word(s)" if item.false_positive else ""
            lines.append(f"{phrase.id:<11} {lang:<4} {phrase.kind:<9} {state} {detail}".rstrip())
            continue
        lines.append(
            f"{phrase.id:<11} {lang:<4} {phrase.kind:<9} {item.ref_words:>5} {item.hyp_words:>5} "
            f"{item.subs:>3} {item.dels:>3} {item.ins:>3} {_percent(item.wer):>8} {_percent(item.cer):>8}"
        )
    speech = [item for item in measures if item.phrase.kind == SPEECH and item.status == "measured"]
    non_speech = [item for item in measures
                  if item.phrase.kind != SPEECH and item.status == "measured"]
    lines.append("")
    if speech:
        ref_words = sum(item.ref_words for item in speech)
        errors = sum(item.subs + item.dels + item.ins for item in speech)
        ref_chars = sum(item.ref_chars for item in speech)
        cer_errors = sum(item.cer_errors for item in speech)
        lines.append(
            f"TOTAL speech ({len(speech)} file(s)): WER {_percent(ratio(errors, ref_words))} "
            f"({errors}/{ref_words} words); CER {_percent(ratio(cer_errors, ref_chars))} "
            f"({cer_errors}/{ref_chars} chars)"
        )
    else:
        lines.append("TOTAL speech: no measured speech files")
    if non_speech:
        positives = sum(1 for item in non_speech if item.false_positive)
        lines.append(
            f"NON-SPEECH ({len(non_speech)} file(s)): "
            f"{positives} false positive(s) of {len(non_speech)}"
        )
    missing = [item for item in measures if item.status == "missing"]
    errors = [item for item in measures if item.status == "error"]
    if missing:
        lines.append("")
        lines.append("missing recordings: " + ", ".join(item.phrase.file for item in missing))
        lines.append("record them first; instructions are in the manifest and README.ru.md")
    if errors:
        lines.append("")
        lines.append("unreadable recordings: " + ", ".join(item.phrase.file for item in errors))
    return "\n".join(lines)


def _result_payload(measures: list[Measure], manifest_path: Path, engine: dict | None) -> dict:
    speech = [item for item in measures if item.phrase.kind == SPEECH and item.status == "measured"]
    non_speech = [item for item in measures
                  if item.phrase.kind != SPEECH and item.status == "measured"]
    ref_words = sum(item.ref_words for item in speech)
    errors = sum(item.subs + item.dels + item.ins for item in speech)
    ref_chars = sum(item.ref_chars for item in speech)
    cer_errors = sum(item.cer_errors for item in speech)
    return {
        "manifest": str(manifest_path),
        "engine": engine,
        "files": [
            {
                "id": item.phrase.id,
                "file": item.phrase.file,
                "language": item.phrase.language,
                "kind": item.phrase.kind,
                "tags": list(item.phrase.tags),
                "status": item.status,
                "error": item.error,
                "reference": item.phrase.text,
                "hypothesis": item.hypothesis,
                "ref_words": item.ref_words,
                "hyp_words": item.hyp_words,
                "subs": item.subs,
                "dels": item.dels,
                "ins": item.ins,
                "wer": item.wer,
                "ref_chars": item.ref_chars,
                "cer_errors": item.cer_errors,
                "cer": item.cer,
                "false_positive": item.false_positive,
            }
            for item in measures
        ],
        "speech": {
            "files": len(speech),
            "ref_words": ref_words,
            "errors": errors,
            "wer": ratio(errors, ref_words),
            "ref_chars": ref_chars,
            "cer_errors": cer_errors,
            "cer": ratio(cer_errors, ref_chars),
        },
        "non_speech": {
            "files": len(non_speech),
            "false_positives": sum(1 for item in non_speech if item.false_positive),
        },
        "missing": [item.phrase.file for item in measures if item.status == "missing"],
        "errors": [item.phrase.file for item in measures if item.status == "error"],
    }


def _write_json(path: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    Path(path).write_text(text, encoding="utf-8")


def _manifest_problems(payload: dict) -> int:
    problems = validate_manifest(payload)
    for problem in problems:
        print(f"wer_cer: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _engine_args(args: argparse.Namespace) -> tuple[dict, str]:
    info = {"model": args.model, "device": args.device, "compute_type": args.compute_type}
    note = f"{args.model} ({args.device}, {args.compute_type})"
    return info, note


def run_corpus(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    try:
        payload = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f"wer_cer: {exc}", file=sys.stderr)
        return 1
    if _manifest_problems(payload):
        return 1
    phrases = parse_phrases(payload)
    engine = None
    engine_info = None
    engine_note = ""
    if args.transcribe:
        engine_info, engine_note = _engine_args(args)
        if any((manifest_path.parent / phrase.file).is_file() for phrase in phrases):
            try:
                engine = _load_engine(args.model, args.device, args.compute_type)
            except Exception as exc:  # noqa: BLE001 - the engine load is third-party
                print(f"wer_cer: cannot load the engine: {exc}", file=sys.stderr)
                return 1
        else:
            engine_note = "not loaded (no recordings yet)"
    measures: list[Measure] = []
    for phrase in phrases:
        audio = manifest_path.parent / phrase.file
        if not audio.is_file():
            measures.append(Measure(phrase=phrase, status="missing"))
            continue
        try:
            if args.hyp_dir:
                hypothesis = _read_hypothesis(Path(args.hyp_dir), phrase)
                if hypothesis is None:
                    measures.append(Measure(phrase=phrase, status="missing"))
                    continue
            else:
                hypothesis = _transcribe(engine, audio, phrase.language)
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the report
            measures.append(Measure(phrase=phrase, status="error", error=str(exc)))
            continue
        measures.append(measure(phrase, hypothesis))
    print(render_report(measures, manifest_path, engine_note))
    if args.json:
        _write_json(args.json, _result_payload(measures, manifest_path, engine_info))
    failed = [item for item in measures if item.status in ("missing", "error")]
    if failed:
        print(f"wer_cer: {len(failed)} recording(s) missing or unreadable", file=sys.stderr)
        return 2
    return 0


def run_baseline(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    try:
        payload = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f"wer_cer: {exc}", file=sys.stderr)
        return 1
    if _manifest_problems(payload):
        return 1
    try:
        baseline = parse_baseline(payload)
    except ManifestError as exc:
        print(f"wer_cer: {exc}", file=sys.stderr)
        return 1
    audio = (manifest_path.parent / str(baseline["file"])).resolve()
    if not audio.is_file():
        print(f"wer_cer: baseline file is missing: {audio}", file=sys.stderr)
        return 2
    engine_info, engine_note = _engine_args(args)
    try:
        engine = _load_engine(args.model, args.device, args.compute_type)
    except Exception as exc:  # noqa: BLE001 - the engine load is third-party
        print(f"wer_cer: cannot load the engine: {exc}", file=sys.stderr)
        return 1
    try:
        hypothesis = _transcribe(engine, audio, str(baseline["language"]))
    except Exception as exc:  # noqa: BLE001 - report the decoder error, not a traceback
        print(f"wer_cer: baseline failed: {exc}", file=sys.stderr)
        return 1
    phrase = Phrase(
        id="baseline",
        file=str(baseline["file"]),
        language=str(baseline["language"]),
        kind=SPEECH,
        tags=(),
        text=str(baseline["text"]),
    )
    item = measure(phrase, hypothesis)
    lines = [
        "WER/CER baseline",
        f"file: {audio}",
        f"engine: {engine_note}",
        f"reference: {phrase.text}",
        f"hypothesis: {hypothesis}",
        f"WER {_percent(item.wer)} (S {item.subs}, D {item.dels}, I {item.ins} / "
        f"{item.ref_words} words)",
        f"CER {_percent(item.cer)} ({item.cer_errors} / {item.ref_chars} chars)",
    ]
    source = str(baseline.get("source", "")).strip()
    if source:
        lines.append(f"source: {source}")
    print("\n".join(lines))
    if args.json:
        _write_json(args.json, _result_payload([item], manifest_path, engine_info))
    return 0


def _check(name: str, condition: bool, detail: str = "") -> bool:
    print(("PASS " if condition else "FAIL ") + name + (f": {detail}" if detail and not condition else ""))
    return condition


def self_test() -> int:
    """Offline gate: normalisation, arithmetic and the manifest shape."""
    checks: list[bool] = []
    ru_mir = "\u043c\u0438\u0440"
    checks.append(_check(
        "normalize ru: case, punctuation, yo",
        normalize("\u0401\u0436\u0438\u043a, \u041c\u0418\u0420!", "ru")
        == "\u0435\u0436\u0438\u043a " + ru_mir,
        normalize("\u0401\u0436\u0438\u043a, \u041c\u0418\u0420!", "ru"),
    ))
    checks.append(_check(
        "normalize en: case and punctuation",
        normalize("Hello, WORLD!", "en") == "hello world",
        normalize("Hello, WORLD!", "en"),
    ))
    checks.append(_check(
        "numbers en: 47 equals forty seven",
        normalize("Order 47", "en") == normalize("Order forty seven", "en"),
        normalize("Order 47", "en"),
    ))
    checks.append(_check(
        "numbers ru: 12 equals dvenadtsat",
        normalize("\u041d\u043e\u043c\u0435\u0440 12", "ru")
        == normalize("\u041d\u043e\u043c\u0435\u0440 \u0434\u0432\u0435\u043d\u0430\u0434\u0446\u0430\u0442\u044c", "ru"),
        normalize("\u041d\u043e\u043c\u0435\u0440 12", "ru"),
    ))
    checks.append(_check(
        "digits glued to letters stay",
        normalize("large-v3-turbo", "en") == "large v3 turbo"
        and "v3" in normalize("v3", "en"),
        normalize("large-v3-turbo", "en"),
    ))
    checks.append(_check(
        "WER: one substitution in three words",
        measure(_phrase("a b c"), "a x c").wer == 1 / 3,
    ))
    deletion = measure(_phrase("a b c"), "a c")
    checks.append(_check(
        "WER: one deletion counted as D",
        deletion.dels == 1 and deletion.ins == 0 and deletion.subs == 0,
    ))
    insertion = measure(_phrase("a b"), "a b c")
    checks.append(_check(
        "WER: one insertion counted as I",
        insertion.ins == 1 and insertion.dels == 0 and insertion.subs == 0,
    ))
    checks.append(_check(
        "CER: one wrong character in four",
        measure(_phrase("abcd"), "abxd").cer == 1 / 4,
    ))
    silence = Phrase("silence", "silence.wav", "", "silence", ("silence",), "")
    checks.append(_check(
        "non-speech: empty output is clean, any text is a false positive",
        measure(silence, "").false_positive is False
        and measure(silence, "").wer == 0.0
        and measure(silence, "hello").false_positive is True
        and measure(silence, "hello").wer == 1.0,
    ))
    try:
        payload = load_manifest(DEFAULT_MANIFEST)
    except ManifestError as exc:
        checks.append(_check("manifest loads", False, str(exc)))
        payload = {}
    problems = validate_manifest(payload) if payload else ["manifest not loaded"]
    checks.append(_check(
        f"manifest: {PHRASE_COUNT} phrases, all sections, baseline",
        not problems,
        "; ".join(problems),
    ))
    phrases = parse_phrases(payload) if payload else []
    sample = Measure(phrase=phrases[0], status="measured") if phrases else Measure(
        phrase=silence, status="measured")
    report = render_report([sample], DEFAULT_MANIFEST)
    checks.append(_check(
        "report: per-file line and speech total",
        sample.phrase.id in report and "TOTAL speech" in report,
        report,
    ))
    failed = sum(1 for ok in checks if not ok)
    print(f"SELF-TEST {'OK' if not failed else 'FAILED'} ({len(checks)} checks)")
    return 0 if not failed else 1


def _phrase(text: str) -> Phrase:
    return Phrase("sample", "sample.wav", "en", SPEECH, (), text)


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure WER/CER of the local ASR against tests/wer/manifest.json.",
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--transcribe", action="store_true",
                        help="recognise the recordings with the product ASR")
    source.add_argument("--hyp-dir", default="",
                        help="read <stem>.txt hypotheses from this directory")
    parser.add_argument("--baseline", action="store_true",
                        help="measure test_ru.wav from the manifest baseline")
    parser.add_argument("--self-test", action="store_true",
                        help="offline gate: normalisation, arithmetic, manifest")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--json", default="", help="write the same report as JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    if args.self_test:
        return self_test()
    if args.baseline:
        return run_baseline(args)
    if not args.transcribe and not args.hyp_dir:
        print("wer_cer: choose --transcribe or --hyp-dir (or --self-test / --baseline)",
              file=sys.stderr)
        return 2
    return run_corpus(args)


if __name__ == "__main__":
    sys.exit(main())
