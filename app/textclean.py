"""Optional cleanup after recognition: numbers, filler words, capitals.

The option ``text_cleanup`` is off by default. When it is on, the finished
phrase passes three deterministic steps, in this order. The service calls it
after the dictionary, the text rules and the snippets (their output is what
the person sees) and after the voice action match, so a command phrase is
compared as it was spoken. The panel draft and the insertion both receive the
cleaned text, and the next take inherits it as context:

1. numbers and dates spoken as words become digits through wetext's inverse
   text normalization (ru and en). Only a run of number words (a date's month
   included) goes to wetext, so the word next to a number (a currency or a
   noun) keeps the spelling it was spoken with;
2. standalone filler words are dropped, never a piece of a word;
3. the first letter of the text and of every sentence is uppercased.

Identifiers (snake_case, camelCase, dotted.paths), URLs, e-mail addresses,
filesystem paths and backticked spans are protected: no step touches them.
wetext is imported lazily on the first number run and loaded once per language
per process; if it fails, the words stay as spoken and the other steps still
run. Nothing here rewrites a phrase with a model. Russian literals are written
as \\u escapes so the source stays ASCII (project rule).
"""
import re

# -- protection -------------------------------------------------------------

# Code and names survive every step: backticked spans, URLs, e-mail
# addresses, filesystem paths, dotted paths, snake_case and camelCase.
_PROTECTED_RX = re.compile(
    r"`[^`]+`"
    r"|(?<![\w@])(?:https?://|www\.)[^\s<>`]+"
    r"|(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|(?<![\w])[A-Za-z]:[\\/][^\s,;:!?\u2026]+"
    r"|(?<![\w])[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+"
    r"|(?<![\w])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"|(?<![\w])[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]*"
    r"|(?<![\w])[A-Za-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*"
)
# Sentence punctuation never belongs to a protected span.
_TRAILING = ".,;:!?)\u2026"


def _spans(text):
    """(start, end) of every protected span, sentence punctuation excluded."""
    found = []
    for match in _PROTECTED_RX.finditer(text):
        start, end = match.span()
        while end > start and text[end - 1] in _TRAILING:
            end -= 1
        if end > start:
            found.append((start, end))
    return found


def _on_free(text, rule):
    """Apply ``rule`` to the text between protected spans only."""
    spans = _spans(text)
    if not spans:
        return rule(text)
    parts = []
    last = 0
    for start, end in spans:
        parts.append(rule(text[last:start]))
        parts.append(text[start:end])
        last = end
    parts.append(rule(text[last:]))
    return "".join(parts)


def _map_free(text, rule):
    """Like ``_on_free`` for a rule that reports (text, changed)."""
    spans = _spans(text)
    parts = []
    last = 0
    changed = False
    for start, end in spans:
        chunk, hit = rule(text[last:start])
        parts.append(chunk)
        parts.append(text[start:end])
        changed = changed or hit
        last = end
    chunk, hit = rule(text[last:])
    parts.append(chunk)
    return "".join(parts), changed or hit


# -- numbers and dates ------------------------------------------------------

# Words that may stand inside one spoken number: cardinal components, the
# ordinal forms a spoken date uses, and the genitive month names that follow
# them. A run of these tokens goes to wetext whole; a word outside the run
# (a currency, a noun) keeps its spelling and its case.
_RU_NUMBER_WORDS = frozenset((
    "\u043d\u043e\u043b\u044c",
    "\u043e\u0434\u0438\u043d",
    "\u043e\u0434\u043d\u0430",
    "\u043e\u0434\u043d\u043e",
    "\u0434\u0432\u0430",
    "\u0434\u0432\u0435",
    "\u0442\u0440\u0438",
    "\u0447\u0435\u0442\u044b\u0440\u0435",
    "\u043f\u044f\u0442\u044c",
    "\u0448\u0435\u0441\u0442\u044c",
    "\u0441\u0435\u043c\u044c",
    "\u0432\u043e\u0441\u0435\u043c\u044c",
    "\u0434\u0435\u0432\u044f\u0442\u044c",
    "\u0434\u0435\u0441\u044f\u0442\u044c",
    "\u043e\u0434\u0438\u043d\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0434\u0432\u0435\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0442\u0440\u0438\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0447\u0435\u0442\u044b\u0440\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u043f\u044f\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0448\u0435\u0441\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0441\u0435\u043c\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0432\u043e\u0441\u0435\u043c\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0434\u0435\u0432\u044f\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c",
    "\u0442\u0440\u0438\u0434\u0446\u0430\u0442\u044c",
    "\u0441\u043e\u0440\u043e\u043a",
    "\u043f\u044f\u0442\u044c\u0434\u0435\u0441\u044f\u0442",
    "\u0448\u0435\u0441\u0442\u044c\u0434\u0435\u0441\u044f\u0442",
    "\u0441\u0435\u043c\u044c\u0434\u0435\u0441\u044f\u0442",
    "\u0432\u043e\u0441\u0435\u043c\u044c\u0434\u0435\u0441\u044f\u0442",
    "\u0434\u0435\u0432\u044f\u043d\u043e\u0441\u0442\u043e",
    "\u0441\u0442\u043e",
    "\u0434\u0432\u0435\u0441\u0442\u0438",
    "\u0442\u0440\u0438\u0441\u0442\u0430",
    "\u0447\u0435\u0442\u044b\u0440\u0435\u0441\u0442\u0430",
    "\u043f\u044f\u0442\u044c\u0441\u043e\u0442",
    "\u0448\u0435\u0441\u0442\u044c\u0441\u043e\u0442",
    "\u0441\u0435\u043c\u044c\u0441\u043e\u0442",
    "\u0432\u043e\u0441\u0435\u043c\u044c\u0441\u043e\u0442",
    "\u0434\u0435\u0432\u044f\u0442\u044c\u0441\u043e\u0442",
    "\u0442\u044b\u0441\u044f\u0447\u0430",
    "\u0442\u044b\u0441\u044f\u0447\u0438",
    "\u0442\u044b\u0441\u044f\u0447",
    "\u0442\u044b\u0441\u044f\u0447\u0443",
    "\u043c\u0438\u043b\u043b\u0438\u043e\u043d",
    "\u043c\u0438\u043b\u043b\u0438\u043e\u043d\u0430",
    "\u043c\u0438\u043b\u043b\u0438\u043e\u043d\u043e\u0432",
    "\u043c\u0438\u043b\u043b\u0438\u0430\u0440\u0434",
    "\u043c\u0438\u043b\u043b\u0438\u0430\u0440\u0434\u0430",
    "\u043c\u0438\u043b\u043b\u0438\u0430\u0440\u0434\u043e\u0432",
    "\u043f\u0435\u0440\u0432\u043e\u0433\u043e",
    "\u0432\u0442\u043e\u0440\u043e\u0433\u043e",
    "\u0442\u0440\u0435\u0442\u044c\u0435\u0433\u043e",
    "\u0447\u0435\u0442\u0432\u0435\u0440\u0442\u043e\u0433\u043e",
    "\u0447\u0435\u0442\u0432\u0451\u0440\u0442\u043e\u0433\u043e",
    "\u043f\u044f\u0442\u043e\u0433\u043e",
    "\u0448\u0435\u0441\u0442\u043e\u0433\u043e",
    "\u0441\u0435\u0434\u044c\u043c\u043e\u0433\u043e",
    "\u0432\u043e\u0441\u044c\u043c\u043e\u0433\u043e",
    "\u0434\u0435\u0432\u044f\u0442\u043e\u0433\u043e",
    "\u0434\u0435\u0441\u044f\u0442\u043e\u0433\u043e",
    "\u043e\u0434\u0438\u043d\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0434\u0432\u0435\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0442\u0440\u0438\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0447\u0435\u0442\u044b\u0440\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u043f\u044f\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0448\u0435\u0441\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0441\u0435\u043c\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0432\u043e\u0441\u0435\u043c\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0434\u0435\u0432\u044f\u0442\u043d\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
    "\u0442\u0440\u0438\u0434\u0446\u0430\u0442\u043e\u0433\u043e",
))

_RU_MONTHS = frozenset((
    "\u044f\u043d\u0432\u0430\u0440\u044f",
    "\u0444\u0435\u0432\u0440\u0430\u043b\u044f",
    "\u043c\u0430\u0440\u0442\u0430",
    "\u0430\u043f\u0440\u0435\u043b\u044f",
    "\u043c\u0430\u044f",
    "\u0438\u044e\u043d\u044f",
    "\u0438\u044e\u043b\u044f",
    "\u0430\u0432\u0433\u0443\u0441\u0442\u0430",
    "\u0441\u0435\u043d\u0442\u044f\u0431\u0440\u044f",
    "\u043e\u043a\u0442\u044f\u0431\u0440\u044f",
    "\u043d\u043e\u044f\u0431\u0440\u044f",
    "\u0434\u0435\u043a\u0430\u0431\u0440\u044f",
))

_EN_NUMBER_WORDS = frozenset((
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
    "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
    "nineteenth", "twentieth", "thirtieth",
))

_EN_MONTHS = frozenset((
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
))

_EN_CONNECTORS = frozenset(("of", "the", "and"))

_VOCAB = {
    "ru": (_RU_NUMBER_WORDS | _RU_MONTHS, frozenset()),
    "en": (_EN_NUMBER_WORDS | _EN_MONTHS, _EN_CONNECTORS),
}

_TOKEN_RX = re.compile(r"[^\W\d_]+")
# wetext is case-sensitive; a lowercased month keeps its capital in English.
_MONTH_RX = re.compile(
    r"(?<!\w)(?:" + "|".join(sorted(_EN_MONTHS)) + r")(?!\w)",
    re.IGNORECASE)

# wetext imports lazily: a process that never converts a number never loads
# the library. One normalizer per language, a failed load remembered as None.
_NORMALIZERS = {}


def _load_normalizer(lang):
    """Build one wetext ITN normalizer; the seam tests replace."""
    from wetext import Normalizer
    return Normalizer(lang=lang, operator="itn")


def _normalizer(lang):
    """The language's ITN normalizer, loaded once per process, or None."""
    if lang not in _NORMALIZERS:
        try:
            _NORMALIZERS[lang] = _load_normalizer(lang)
        except Exception:  # noqa: BLE001 - a missing library keeps the words
            _NORMALIZERS[lang] = None
    return _NORMALIZERS[lang]


def _case_forms(span, lang):
    """The span as spoken and, behind it, its lowercased forms.

    wetext reads a capital as part of the word: "Двадцать три" is not a
    numeral for it, "двадцать три" is. The original form is tried first, so
    a successful reading is never retold in another case.
    """
    forms = [span]
    if lang == "ru":
        forms.append(span.lower())
    else:
        forms.append(span[:1].lower() + span[1:])
        forms.append(span.lower())
    out = []
    for form in forms:
        if form and form not in out:
            out.append(form)
    return out


def _restore_case(result, span, lang):
    """A lowercased retelling keeps the sentence capital and English months."""
    if result[:1].islower() and span[:1].isupper():
        result = result[:1].upper() + result[1:]
    if lang == "en":
        result = _MONTH_RX.sub(
            lambda match: match.group(0).capitalize(), result)
    return result


def _normalize(span, lang):
    """One spoken number run as wetext writes it; unchanged on a failure."""
    normalizer = _normalizer(lang)
    if normalizer is None:
        return span
    for index, candidate in enumerate(_case_forms(span, lang)):
        try:
            result = str(normalizer.normalize(candidate)).strip()
        except Exception:  # noqa: BLE001 - a failed run keeps the words as spoken
            return span
        if result and result != candidate:
            if index == 0:
                return result
            return _restore_case(result, span, lang)
    return span


def _runs(tokens, text, words, connectors):
    """Consecutive allowed tokens on one line, a hyphen keeping a run."""
    runs = []
    run = []
    for start, end, word in tokens:
        if word not in words and word not in connectors:
            if run:
                runs.append(run)
                run = []
            continue
        if run and text[run[-1][1]:start].strip() not in ("", "-"):
            runs.append(run)
            run = []
        run.append((start, end, word))
    if run:
        runs.append(run)
    return runs


def _convert_numbers(chunk, lang):
    """Replace every spoken number run with wetext's digits; (text, changed)."""
    vocab = _VOCAB.get(lang)
    if vocab is None:
        return chunk, False
    words, connectors = vocab
    tokens = [(match.start(), match.end(), match.group(0).casefold())
              for match in _TOKEN_RX.finditer(chunk)]
    parts = []
    last = 0
    changed = False
    for run in _runs(tokens, chunk, words, connectors):
        if not any(word in words for _start, _end, word in run):
            continue
        start, end = run[0][0], run[-1][1]
        converted = _normalize(chunk[start:end], lang)
        if converted == chunk[start:end]:
            continue
        parts.append(chunk[last:start])
        parts.append(converted)
        last = end
        changed = True
    if not changed:
        return chunk, False
    parts.append(chunk[last:])
    return "".join(parts), True


# -- filler words -----------------------------------------------------------

# Hesitation sounds are never a word of their own: they go wherever they
# stand. The other words are fillers only when punctuation sets them off;
# a demonstrative opener, "type of" and "I like it" keep their meaning.
_HESITATIONS = (
    r"\u044d\u044d+", r"\u044d\u043c+", r"\u043c\u043c+", r"um+", r"uh+", r"er+", r"erm+", r"hmm+",
)
_RU_FILLER_RULES = (
    (r"\u043d\u0443", True),
    (r"\u043a\u0430\u043a[ \t]+\u0431\u044b", False),
    (r"\u0442\u0438\u043f\u0430", False),
    (r"\u0432\u043e\u0442", False),
    (r"\u043a\u043e\u0440\u043e\u0447\u0435", False),
)
_EN_FILLER_RULES = (
    (r"like", False),
    (r"you[ \t]+know", False),
)
# A comma, a semicolon, a colon or a dash sets a filler word off.
_DELIMITERS = ",;:\u2014\u2013"
_MAX_PASSES = 8


def _filler_rx(pattern):
    return re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)", re.IGNORECASE)


def _rules(entries, always):
    return tuple((_filler_rx(pattern), allow_start, always)
                 for pattern, allow_start in entries)


_HESITATION_RULES = _rules(
    ((pattern, False) for pattern in _HESITATIONS), True)
_RU_RULES = _rules(_RU_FILLER_RULES, False)
_EN_RULES = _rules(_EN_FILLER_RULES, False)


def _phrase_start(text, start):
    """Only spaces (or a line start) stand between ``start`` and the phrase."""
    i = start - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i < 0 or text[i] in ".!?\u2026\n"


def _punct_before(text, start):
    """Index and character of the first non-space before ``start``."""
    i = start - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i, (text[i] if i >= 0 else "")


def _punct_after(text, end):
    """Index and character of the first non-space after ``end``."""
    i = end
    while i < len(text) and text[i] in " \t":
        i += 1
    return i, (text[i] if i < len(text) else "")


def _allowed_filler(text, start, end, always, allow_start):
    """A hesitation goes anywhere; a word filler needs a setting-off mark."""
    if always:
        return True
    _left, left_char = _punct_before(text, start)
    _right, right_char = _punct_after(text, end)
    if left_char and left_char in _DELIMITERS:
        return True
    if right_char and right_char in _DELIMITERS:
        return True
    return allow_start and _phrase_start(text, start)


def _strip_one(text, start, end, keep_one):
    """Drop the filler; a hesitation between two marks leaves the closing one."""
    left, left_char = _punct_before(text, start)
    right, right_char = _punct_after(text, end)
    left_delim = bool(left_char) and left_char in _DELIMITERS
    right_delim = bool(right_char) and right_char in _DELIMITERS
    cut_start, cut_end = start, end
    if left_delim and right_delim:
        cut_start = left
        if not keep_one:
            cut_end = right + 1
    elif right_delim:
        cut_end = right + 1
    elif left_delim:
        cut_start = left
    return text[:cut_start] + text[cut_end:]


def _first_strippable(text, rx, allow_start, always):
    for match in rx.finditer(text):
        start, end = match.span()
        if _allowed_filler(text, start, end, always, allow_start):
            return start, end
    return None


def _strip_all(text, rules):
    """Remove every allowed match of every rule; bounded passes per rule."""
    for rx, allow_start, always in rules:
        for _ in range(_MAX_PASSES):
            found = _first_strippable(text, rx, allow_start, always)
            if found is None:
                break
            result = _strip_one(text, found[0], found[1], always)
            if result == text:
                break
            text = result
    return text


def _strip_fillers(chunk, lang):
    """Drop the fillers of the pinned language; (text, changed)."""
    original = chunk
    chunk = _strip_all(chunk, _HESITATION_RULES)
    if lang == "ru":
        chunk = _strip_all(chunk, _RU_RULES)
    elif lang == "en":
        chunk = _strip_all(chunk, _EN_RULES)
    return chunk, chunk != original


# -- spacing and capitals ---------------------------------------------------

_DOUBLE_COMMA_RX = re.compile(r",[ \t]*,+")
_COMMA_BEFORE_END_RX = re.compile(r",[ \t]*([.!?\u2026])")
_SPACE_BEFORE_PUNCT_RX = re.compile(r"[ \t]+([,.;:!?\u2026])")
_DOUBLE_SPACE_RX = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_NEWLINE_RX = re.compile(r"[ \t]+\n")
_LEADING_JUNK_RX = re.compile(r"^[ \t]*[,;:]+[ \t]*")
_TRAILING_JUNK_RX = re.compile(r"[ \t]*[,;:]+[ \t]*$")


def _repair(chunk):
    """No doubled spaces, no orphan punctuation after a dropped filler."""
    chunk = _DOUBLE_COMMA_RX.sub(",", chunk)
    chunk = _COMMA_BEFORE_END_RX.sub(r"\1", chunk)
    chunk = _SPACE_BEFORE_PUNCT_RX.sub(r"\1", chunk)
    chunk = _DOUBLE_SPACE_RX.sub(" ", chunk)
    return _SPACE_BEFORE_NEWLINE_RX.sub("\n", chunk)


def _repair_edges(text):
    """The text itself never opens or closes with orphan punctuation."""
    text = _LEADING_JUNK_RX.sub("", text)
    text = _TRAILING_JUNK_RX.sub("", text)
    return text.strip(" \t")


# Characters a sentence may open with before its first letter.
_OPENERS = "\"'\u00ab\u201c\u201e([{\u2013\u2014-"


def _capitalize(text):
    """Uppercase the first letter of the text and of every sentence."""
    if not text:
        return text, False
    protected = bytearray(len(text))
    for start, end in _spans(text):
        protected[start:end] = b"\x01" * (end - start)
    chars = list(text)
    at_start = True
    for index, char in enumerate(chars):
        if at_start:
            if char in " \t\n":
                continue
            if protected[index]:
                at_start = False
                continue
            if char in _OPENERS:
                continue
            if char.isalpha():
                chars[index] = char.upper()
            at_start = False
        elif not protected[index] and char in ".!?\u2026\n":
            at_start = True
    result = "".join(chars)
    return result, result != text


# -- pipeline ---------------------------------------------------------------


def apply(text, cfg):
    """Run the three steps when ``text_cleanup`` is on; (text, changes).

    Off (the default) returns the text byte for byte. The order of the steps
    is the module docstring's; the service calls it after the dictionary, the
    text rules and the snippets and before the panel draft and the insertion.
    """
    if not text:
        return text, 0
    if not isinstance(cfg, dict) or not cfg.get("text_cleanup", False):
        return text, 0
    language = str(cfg.get("language") or "").strip().lower()
    changes = 0
    cleaned, converted = _map_free(
        text, lambda chunk: _convert_numbers(chunk, language))
    changes += int(converted)
    cleaned, stripped = _map_free(
        cleaned, lambda chunk: _strip_fillers(chunk, language))
    changes += int(stripped)
    cleaned = _on_free(cleaned, _repair)
    cleaned = _repair_edges(cleaned)
    cleaned, capitalized = _capitalize(cleaned)
    changes += int(capitalized)
    return cleaned, changes
