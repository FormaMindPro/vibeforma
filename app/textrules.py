"""Rule-based clean-up after recognition: backtrack and spoken lists.

Both rules are deterministic and offline; no model and no network. They run
only when the user turned on ``text_rules`` (off by default, independent of
snippets).

Backtrack: a spoken correction ("no, more precisely", "I mean") replaces the
abandoned phrase from its start with the new wording; without new wording the
abandoned thought is dropped. Spoken enumerators ("one, ..., two, ...") become
a numbered list. Russian literals are written as \\u escapes so the source
stays ASCII (project rule).
"""
import re

# Spoken correction triggers, Russian, as regex fragments; word boundaries are
# added around the whole alternation.
_TRIGGERS = (
    # no, more precisely
    "\u043d\u0435\u0442\\s*,?\\s*\u0442\u043e\u0447\u043d\u0435\u0435",
    # that is, no
    "\u0442\u043e\\s+\u0435\u0441\u0442\u044c\\s+\u043d\u0435\u0442",
    # actually (with the hyphen)
    "\u0432\u043e\u043e\u0431\u0449\u0435\\s*-\\s*\u0442\u043e",
    # sorry, I meant
    "\u043f\u0440\u043e\u0441\u0442\u0438\\s*,?\\s*\u044f\\s+\u0438\u043c\u0435\u043b"
    "\\s+\u0432\\s+\u0432\u0438\u0434\u0443",
    # rather
    "\u0432\u0435\u0440\u043d\u0435\u0435",
)
_TRIGGER_RX = re.compile(
    r"(?<!\w)(?:" + "|".join(_TRIGGERS) + r")(?!\w)", re.IGNORECASE)

# A phrase (sentence) ends at one of these; a new line also starts one.
_SENTENCE_END = ".!?;\n\u2026"
# Punctuation a new wording may start with after the trigger.
_LEADING_NOISE = re.compile(r"^[\s,.;:!?\u2026\u2014\u2013-]+")
_WORD_RX = re.compile(r"\w")
_MAX_PASSES = 8

# Speech phrases that introduce a list item. Each family starts at the first
# item and may run up to three; "strict" families also need punctuation after
# the marker, so "one friend of mine" never becomes a list.
_FAMILIES = (
    # one, two, three
    (True, (
        "\u043e\u0434\u0438\u043d|\u043e\u0434\u043d\u0430",
        "\u0434\u0432\u0430|\u0434\u0432\u0435",
        "\u0442\u0440\u0438",
    )),
    # first, second, third (neuter: "first, ...")
    (True, (
        "\u043f\u0435\u0440\u0432\u043e\u0435",
        "\u0432\u0442\u043e\u0440\u043e\u0435",
        "\u0442\u0440\u0435\u0442\u044c\u0435",
    )),
    # first(ly), second(ly), third(ly)
    (False, (
        "\u0432\u043e[\\s-]?\u043f\u0435\u0440\u0432\u044b\u0445",
        "\u0432\u043e[\\s-]?\u0432\u0442\u043e\u0440\u044b\u0445",
        "\u0432[\\s-]?\u0442\u0440\u0435\u0442\u044c\u0438\u0445",
    )),
    # item one, item two, item three
    (False, (
        "\u043f\u0443\u043d\u043a\u0442\\s+(?:\u043e\u0434\u0438\u043d|\u043e\u0434\u043d\u0430)",
        "\u043f\u0443\u043d\u043a\u0442\\s+(?:\u0434\u0432\u0430|\u0434\u0432\u0435)",
        "\u043f\u0443\u043d\u043a\u0442\\s+\u0442\u0440\u0438",
    )),
)
# Where a marker may stand: at the text start or after a phrase boundary.
_PHRASE_LEAD = ".!?;:,\n\u2026"
# Space characters that do not start a new line.
_INLINE_SPACE = " \t\r"


def _last_boundary(text, end):
    """Index just after the last sentence terminator before ``end``."""
    for i in range(end - 1, -1, -1):
        if text[i] in _SENTENCE_END:
            return i + 1
    return 0


def _segment_start(text, start):
    """Where the abandoned phrase starts for a trigger at ``start``."""
    cut = _last_boundary(text, start)
    if text[cut:start].strip():
        return cut
    if cut:
        # The trigger opens its own phrase, so the abandoned one is before it.
        return _last_boundary(text, cut - 1)
    return 0


def _upper_first(text):
    return text[:1].upper() + text[1:]


def _join(prefix, replacement):
    """Glue the untouched prefix to the new wording."""
    if not prefix:
        return _upper_first(replacement)
    if prefix[-1] in ".!?\u2026":
        return prefix + " " + _upper_first(replacement)
    return prefix + " " + replacement


def _find_trigger(text):
    """The first trigger that has a spoken thought before it, or None."""
    for match in _TRIGGER_RX.finditer(text):
        if _WORD_RX.search(text[:match.start()]):
            return match
    return None


def _apply_trigger(text, match):
    start = _segment_start(text, match.start())
    prefix = text[:start].rstrip()
    replacement = _LEADING_NOISE.sub("", text[match.end():]).strip()
    if replacement:
        return _join(prefix, replacement)
    return prefix


def _backtrack(text):
    count = 0
    for _ in range(_MAX_PASSES):
        match = _find_trigger(text)
        if match is None:
            break
        result = _apply_trigger(text, match)
        if result == text:
            break
        text = result
        count += 1
        if not text:
            break
    return text, count


def backtrack(text):
    """Replace or drop the thought before a spoken correction."""
    return _backtrack(text)[0]


def _at_phrase_start(text, pos):
    """The marker stands at the start of a phrase, not inside one."""
    i = pos - 1
    while i >= 0 and text[i] in _INLINE_SPACE:
        i -= 1
    if i < 0 or text[i] == "\n":
        return True
    return text[i] in _PHRASE_LEAD


def _marker_rx(token, strict):
    if strict:
        separator = r"\s*[,;:.\u2026\u2014\u2013-]\s*"
    else:
        separator = r"\s*[,;:.\u2026\u2014\u2013-]?\s+"
    return re.compile(
        r"(?<!\w)(?:" + token + r")(?!\w)(?:" + separator + r")",
        re.IGNORECASE)


def _markers(text, tokens, strict):
    """(marker start, family position, item start) for every marker found."""
    found = []
    for position, token in enumerate(tokens):
        for match in _marker_rx(token, strict).finditer(text):
            if _at_phrase_start(text, match.start()):
                found.append((match.start(), position, match.end()))
    found.sort()
    return found


def _runs(text, tokens, strict):
    """Consecutive marker runs of two or three items."""
    run = []
    for marker in _markers(text, tokens, strict):
        if marker[1] == 0:
            run = [marker]
        elif run and marker[1] == run[-1][1] + 1:
            run.append(marker)
            if len(run) >= 2:
                yield list(run)
        else:
            run = []


def _list_candidates(text):
    """Runs from every family, earliest first, longest run per start."""
    found = []
    for strict, tokens in _FAMILIES:
        found.extend(_runs(text, tokens, strict))
    found.sort(key=lambda run: (run[0][0], -len(run)))
    seen = set()
    for run in found:
        if run[0][0] in seen:
            continue
        seen.add(run[0][0])
        yield run


def _clean_item(text):
    return text.strip(_INLINE_SPACE + "\n,;:.")


def _last_item_end(text, start):
    """Where the last item ends and the surrounding text resumes."""
    for i in range(start, len(text)):
        if text[i] in ".!?\u2026\n" and text[i + 1:].strip():
            return i, i + 1
    return len(text), len(text)


def _format_run(text, run):
    prefix = text[:run[0][0]].rstrip()
    items = []
    trailing = ""
    for index, marker in enumerate(run):
        if index + 1 < len(run):
            item_end = run[index + 1][0]
        else:
            item_end, tail_start = _last_item_end(text, marker[2])
            trailing = text[tail_start:].strip()
        item = _clean_item(text[marker[2]:item_end])
        if not item:
            return None
        items.append(item)
    body = "\n".join(
        f"{number}. {item}" for number, item in enumerate(items, 1))
    if prefix:
        body = prefix + "\n\n" + body
    if trailing:
        body += "\n\n" + trailing
    return body


def _lists(text):
    for run in _list_candidates(text):
        result = _format_run(text, run)
        if result is not None and result != text:
            return result, 1
    return text, 0


def lists(text):
    """Turn a spoken row of two or three enumerators into a numbered list."""
    return _lists(text)[0]


def apply(text, cfg):
    """Run both rules when ``text_rules`` is on; returns (text, changes)."""
    if not text:
        return text, 0
    if not isinstance(cfg, dict) or not cfg.get("text_rules", False):
        return text, 0
    text, backtracked = _backtrack(text)
    text, listed = _lists(text)
    return text, backtracked + listed
