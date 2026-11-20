"""User dictionary: term replacements after ASR and terms for the ASR prompt.

Entries live in config.json under "dictionary": a list of
{"from": <term as recognized>, "to": <replacement>}. An entry without "to"
only biases the model (its term goes into the initial prompt). Replacement
is literal and case-insensitive; the longest term wins; one pass, so a
replacement is never re-processed by another rule.
"""
import json
import re

MAX_ENTRIES = 500
MAX_FROM = 80
MAX_TO = 200
MAX_PROMPT = 500


def _clean(entry):
    """One entry as {"from", "to"} or None when unusable."""
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        entry = {"from": entry[0], "to": entry[1]}
    if not isinstance(entry, dict):
        return None
    src = str(entry.get("from") or "").strip()
    dst = str(entry.get("to") or "").strip()
    if not src or src == dst:
        return None
    return {"from": src[:MAX_FROM], "to": dst[:MAX_TO]}


def normalize(entries):
    """Clean, de-duplicate (case-insensitive) and cap the entry list."""
    out = []
    seen = set()
    for item in entries or []:
        entry = _clean(item)
        if entry is None:
            continue
        key = entry["from"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def _pattern(term):
    """Literal term with word boundaries where the term has them."""
    body = re.escape(term)
    left = r"(?<!\w)" if term[:1].isalnum() else ""
    right = r"(?!\w)" if term[-1:].isalnum() else ""
    return left + body + right


def _substitute(text, entries):
    """One substitution pass; returns (text, counts by casefolded term)."""
    active = [e for e in normalize(entries) if e["to"]]
    if not text or not active:
        return text, {}
    active.sort(key=lambda e: len(e["from"]), reverse=True)
    table = {e["from"].casefold(): e["to"] for e in active}
    rx = re.compile("|".join(_pattern(e["from"]) for e in active),
                    re.IGNORECASE)
    counts = {}

    def _hit(match):
        key = match.group(0).casefold()
        value = table.get(key)
        if value is None:
            return match.group(0)
        counts[key] = counts.get(key, 0) + 1
        return value

    return rx.sub(_hit, text), counts


def apply(text, entries):
    """Replace dictionary terms; returns (result, number of replacements)."""
    result, counts = _substitute(text, entries)
    return result, sum(counts.values())


def changes(text, entries):
    """What would change in text: [{"from", "to", "count"}] in entry order."""
    _result, counts = _substitute(text, entries)
    out = []
    for entry in normalize(entries):
        if not entry["to"]:
            continue
        n = counts.get(entry["from"].casefold(), 0)
        if n:
            out.append({"from": entry["from"], "to": entry["to"], "count": n})
    return out


def hotword_terms(entries):
    """Terms for the recognition prompt: every entry, replacement or not."""
    return [e["from"] for e in normalize(entries)]


def merge_hotwords(base, entries):
    """Append dictionary terms to the user's hotwords without duplicates."""
    parts = []
    seen = set()
    for chunk in str(base or "").split(","):
        term = chunk.strip()
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        parts.append(term)
    for term in hotword_terms(entries):
        if term.casefold() in seen:
            continue
        seen.add(term.casefold())
        parts.append(term)
    merged = ", ".join(parts)
    return merged[:MAX_PROMPT]


def to_json(entries):
    """Export format: a readable JSON list of {"from", "to"} objects."""
    return json.dumps(normalize(entries), ensure_ascii=False, indent=2)


def from_json(raw):
    """Parse an exported file; raises ValueError on malformed JSON."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("dictionary", [])
    if not isinstance(data, list):
        raise ValueError("dictionary must be a JSON list")
    return normalize(data)
