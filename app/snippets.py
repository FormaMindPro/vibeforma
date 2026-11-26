"""Snippets: spoken trigger phrases replaced with prepared text.

Entries live in config.json under "snippets": a list of
{"trigger": <spoken phrase>, "text": <inserted text>}. Replacement is
literal and case-insensitive and respects word boundaries; the longest
trigger wins, and one pass runs, so a replacement is never re-processed by
another trigger. The text is inserted exactly as written, line breaks
included. Russian frequent words in the validation data are written as
\\u escapes so the source stays ASCII (project rule).
"""
import json

import dictionary

MAX_ENTRIES = 500
MAX_TRIGGER = 80
MAX_TEXT = 2000

# One-word triggers built from these common words fire by accident; the
# editor warns about them instead of blocking the pair.
_FREQUENT_WORDS = frozenset({
    # English
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for",
    "from", "he", "hello", "hi", "i", "in", "is", "it", "me", "my", "no",
    "not", "of", "ok", "okay", "on", "or", "please", "she", "so", "thanks",
    "that", "the", "this", "to", "we", "what", "yes", "you",
    # Russian one-syllable and function words (\u escapes, source stays ASCII)
    "\u0430", "\u0431\u044b", "\u0432", "\u0432\u043e\u0442",
    "\u0432\u0441\u0435", "\u0432\u0441\u0451", "\u0432\u044b", "\u0434\u0430",
    "\u0434\u043b\u044f", "\u0434\u043e", "\u0436\u0435", "\u0437\u0430",
    "\u0438", "\u0438\u0437", "\u0438\u043b\u0438", "\u043a",
    "\u043a\u0430\u043a", "\u043b\u0438", "\u043c\u044b", "\u043d\u0430",
    "\u043d\u0435", "\u043d\u0435\u0442", "\u043d\u043e", "\u043d\u0443",
    "\u043e", "\u043e\u043d", "\u043e\u043d\u0430", "\u043e\u043d\u0438",
    "\u043e\u043d\u043e", "\u043f\u043e", "\u0441", "\u0441\u043e",
    "\u0442\u0430\u043a", "\u0442\u043e", "\u0443", "\u0447\u0442\u043e",
    "\u044d\u0442\u043e", "\u044f",
})


def _clean(entry):
    """One entry as {"trigger", "text"} or None when unusable."""
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        entry = {"trigger": entry[0], "text": entry[1]}
    if not isinstance(entry, dict):
        return None
    trigger = str(entry.get("trigger") or "").strip()
    text = str(entry.get("text") or "").strip()
    if not trigger or not text:
        return None
    if trigger.casefold() == text.casefold():
        return None
    return {"trigger": trigger[:MAX_TRIGGER], "text": text[:MAX_TEXT]}


def normalize(entries):
    """Clean, de-duplicate (case-insensitive trigger) and cap the pair list."""
    out = []
    seen = set()
    for item in entries or []:
        entry = _clean(item)
        if entry is None:
            continue
        key = entry["trigger"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def merge(base, profile):
    """The global snippets with a profile's snippets on top, one per trigger.

    An app profile may redraw a phrase the global list already expands: the
    profile entry wins and the global duplicate is gone, so one window never
    runs two rules for the same trigger. Everything else keeps its order.
    """
    return normalize([*(profile or []), *(base or [])])


def _substitute(text, entries):
    """One substitution pass; returns (text, counts by casefolded trigger)."""
    return dictionary.replace_pairs(text, normalize(entries), "trigger", "text")


def apply(text, entries):
    """Replace snippet triggers; returns (result, number of replacements)."""
    result, counts = _substitute(text, entries)
    return result, sum(counts.values())


def _dictionary_terms(entries):
    """Casefolded "from" values of a dictionary entry list."""
    terms = set()
    for item in entries or []:
        if isinstance(item, dict):
            src = item.get("from")
        elif isinstance(item, (list, tuple)) and item:
            src = item[0]
        else:
            continue
        term = str(src or "").strip().casefold()
        if term:
            terms.add(term)
    return terms


def validate(entries, dictionary=None):
    """Report problems in the raw pair list: [{level, code, trigger}].

    Codes: "duplicate" (the trigger repeats another snippet) and
    "dictionary" (the trigger equals a dictionary "from") are errors;
    "frequent" (a one-word trigger from the common-word list) is a warning.
    The list is not normalized first: duplicates must stay visible when
    reported.
    """
    issues = []
    seen = set()
    terms = _dictionary_terms(dictionary)
    for item in entries or []:
        entry = _clean(item)
        if entry is None:
            continue
        key = entry["trigger"].casefold()
        duplicate = key in seen
        seen.add(key)
        if duplicate:
            issues.append({"level": "error", "code": "duplicate",
                           "trigger": entry["trigger"]})
        if key in terms:
            issues.append({"level": "error", "code": "dictionary",
                           "trigger": entry["trigger"]})
        elif len(entry["trigger"].split()) == 1 and key in _FREQUENT_WORDS:
            issues.append({"level": "warning", "code": "frequent",
                           "trigger": entry["trigger"]})
    return issues


def to_json(entries):
    """Export format: a readable JSON list of {"trigger", "text"} objects."""
    return json.dumps(normalize(entries), ensure_ascii=False, indent=2)


def from_json(raw):
    """Parse an exported file; raises ValueError on malformed JSON."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("snippets", [])
    if not isinstance(data, list):
        raise ValueError("snippets must be a JSON list")
    return normalize(data)
