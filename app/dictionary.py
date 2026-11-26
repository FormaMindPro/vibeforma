"""User dictionary: term replacements after ASR and terms for the ASR prompt.

Entries live in config.json under "dictionary": a list of
{"from": <term as recognized>, "to": <replacement>}. An entry without "to"
only biases the model. Replacement is literal and case-insensitive; the
longest term wins; one pass, so a replacement is never re-processed by
another rule.

The prompt side is ``hotwords``: faster-whisper copies it into the prompt of
every 30 s window, while ``initial_prompt`` seeds the first one only. A pair
therefore teaches the target form (``to``), and the list is ranked by how
often the same correction was made (corrections.json) before it is trimmed
to the library's half-prompt budget.
"""
import json
import re

MAX_ENTRIES = 500
MAX_FROM = 80
MAX_TO = 200
# Hotwords are copied into every window's prompt, where the library truncates
# them only at 448 // 2 = 224 tokens (faster_whisper.transcribe.get_prompt).
# One token of headroom stays for the leading space that the library adds.
MAX_PROMPT = 223


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


def merge(base, profile):
    """The global entries with a profile's entries on top, one per "from".

    An app profile may rename a term the global list already carries: the
    profile entry wins and the global duplicate is gone, so one window never
    runs two rules for the same word. Everything else keeps its order.
    """
    return normalize([*(profile or []), *(base or [])])


def _pattern(term):
    """Literal term with word boundaries where the term has them."""
    body = re.escape(term)
    left = r"(?<!\w)" if term[:1].isalnum() else ""
    right = r"(?!\w)" if term[-1:].isalnum() else ""
    return left + body + right


# ponytail: one literal regex pass shared by dictionary and snippets; ceiling:
# two keys per entry and no backreferences; upgrade: a compiled matcher per list.
def replace_pairs(text, entries, source, target):
    """One literal case-insensitive pass over normalized pairs; the longest wins.

    The dictionary and the snippets share this engine: each names its own
    pair keys. An entry without a target never replaces, and a replacement
    is never re-processed by another entry in the same pass.
    """
    active = [entry for entry in entries if entry[target]]
    if not text or not active:
        return text, {}
    active.sort(key=lambda entry: len(entry[source]), reverse=True)
    table = {entry[source].casefold(): entry[target] for entry in active}
    rx = re.compile("|".join(_pattern(entry[source]) for entry in active),
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
    result, counts = replace_pairs(text, normalize(entries), "from", "to")
    return result, sum(counts.values())


def changes(text, entries):
    """What would change in text: [{"from", "to", "count"}] in entry order."""
    _result, counts = replace_pairs(text, normalize(entries), "from", "to")
    out = []
    for entry in normalize(entries):
        if not entry["to"]:
            continue
        n = counts.get(entry["from"].casefold(), 0)
        if n:
            out.append({"from": entry["from"], "to": entry["to"], "count": n})
    return out


def estimate_tokens(text):
    """A conservative Whisper-BPE token estimate for a hotwords string.

    The real tokenizer arrives only with a loaded model, so selection must
    work before a transcription. Latin words average about four characters
    per token and Cyrillic about two; every word costs at least one token.
    """
    total = 0
    for word in str(text or "").split():
        width = 4 if word.isascii() else 2
        total += max(1, -(-len(word) // width))
    return total


def _weight(weights, term):
    """How often the term was corrected; junk in the store counts as zero."""
    try:
        return max(0, int(weights.get(term.casefold(), 0) or 0))
    except (TypeError, ValueError):
        return 0


def hotword_terms(entries, weights=None):
    """Terms for the recognition prompt: the target form, replacement or not.

    A pair "as heard -> as needed" teaches the model the form it must
    produce, so the ``to`` side goes to the prompt; an entry without one
    keeps its ``from`` (bias only). ``weights`` maps a casefolded term to how
    often that correction was made: the most frequent go first, dictionary
    order breaks ties.
    """
    items = [(entry["to"] or entry["from"], index)
             for index, entry in enumerate(normalize(entries))]
    if isinstance(weights, dict) and weights:
        items.sort(key=lambda item: (-_weight(weights, item[0]), item[1]))
    return [term for term, _index in items]


def _base_terms(base):
    """The user's own hotwords string as separate terms."""
    return [chunk.strip() for chunk in str(base or "").split(",") if chunk.strip()]


def merge_hotwords(base, entries, weights=None, token_counter=None):
    """The user's hotwords plus dictionary target forms, within the budget.

    Every window gets this string, so it must fit the library's half-prompt
    limit. The user's own hotwords come first, then dictionary terms ranked
    by correction frequency; a term that no longer fits is skipped, never cut
    mid-word, and a shorter one later may still fit. ``token_counter`` counts
    one term when the caller has the real tokenizer; ``estimate_tokens``
    stands in before a model is loaded.
    """
    counter = token_counter or estimate_tokens
    parts = []
    seen = set()
    used = 0
    for term in _base_terms(base) + hotword_terms(entries, weights):
        key = term.casefold()
        if key in seen:
            continue
        cost = counter(term) + 1    # the separator before the next term
        if used + cost > MAX_PROMPT:
            continue
        seen.add(key)
        parts.append(term)
        used += cost
    return ", ".join(parts)


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
