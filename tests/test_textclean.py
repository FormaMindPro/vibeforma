"""Opt-in text cleanup: numbers, fillers, capitals, protection (card-254)."""
import importlib.util
import sys
import types
from pathlib import Path

import config
import pytest
import textclean
from vibeforma.service import Service

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app" / "vibeforma" / "service.py"

ON_RU = {"text_cleanup": True, "language": "ru"}
ON_EN = {"text_cleanup": True, "language": "en"}
OFF_RU = {"text_cleanup": False, "language": "ru"}

needs_wetext = pytest.mark.skipif(
    importlib.util.find_spec("wetext") is None,
    reason="wetext is a Windows runtime dependency")


class _Log:
    def __init__(self):
        self.lines = []

    def _write(self, fmt, args):
        self.lines.append(fmt % args if args else fmt)

    def info(self, fmt, *args):
        self._write(fmt, args)

    def warning(self, fmt, *args):
        self._write(fmt, args)

    def debug(self, fmt, *args):
        self._write(fmt, args)

    def exception(self, fmt, *args):
        self._write(fmt, args)


class _Recorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class _ASR:
    def __init__(self, text):
        self.text = text

    def transcribe(self, _buf):
        return self.text


def make_service(**cfg):
    base = {
        "locale": "en",
        "language": "ru",
        "dictionary": [],
        "snippets": [],
        "text_rules": False,
        "text_cleanup": False,
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
    }
    base.update(cfg)
    svc = Service(base, _Log())
    svc._running = True
    return svc


@pytest.fixture
def fresh_normalizers(monkeypatch):
    """An empty per-language cache, so a test controls the loading."""
    monkeypatch.setattr(textclean, "_NORMALIZERS", {})
    return textclean._NORMALIZERS


# -- the option -------------------------------------------------------------


def test_text_cleanup_default_is_off():
    assert config.DEFAULTS["text_cleanup"] is False


@pytest.mark.parametrize("cfg", [{}, {"text_cleanup": False}, None])
def test_apply_is_off_by_default(cfg):
    text = "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430"
    assert textclean.apply(text, cfg) == (text, 0)


@needs_wetext
def test_the_human_verdict_phrase():
    text = "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430"
    cleaned, changes = textclean.apply(text, ON_RU)
    assert cleaned == "23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430"
    assert changes >= 1


# -- numbers and dates ------------------------------------------------------


@needs_wetext
@pytest.mark.parametrize("text, expected", [
    ("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438", "23"),
    ("\u0441\u0442\u043e \u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438", "123"),
    ("\u0442\u044b\u0441\u044f\u0447\u0430 \u0434\u0432\u0435\u0441\u0442\u0438 \u0442\u0440\u0438\u0434\u0446\u0430\u0442\u044c \u0447\u0435\u0442\u044b\u0440\u0435", "1234"),
    ("\u043f\u0435\u0440\u0432\u043e\u0433\u043e \u044f\u043d\u0432\u0430\u0440\u044f \u0434\u0432\u0435 \u0442\u044b\u0441\u044f\u0447\u0438 \u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0447\u0435\u0442\u044b\u0440\u0435",
     "01.01 2024"),
    ("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0435\u0442\u044c\u0435\u0433\u043e \u043c\u0430\u0440\u0442\u0430", "23.03"),
])
def test_russian_numbers_and_dates_become_digits(text, expected):
    assert textclean.apply(text, ON_RU)[0] == expected


@needs_wetext
@pytest.mark.parametrize("text, expected", [
    ("twenty three", "23"),
    ("one thousand two hundred thirty four", "1234"),
    ("January fifth", "January 5th"),
    ("the twenty third of March", "The 23rd of March"),
])
def test_english_numbers_and_dates_become_digits(text, expected):
    assert textclean.apply(text, ON_EN)[0] == expected


@needs_wetext
def test_a_sentence_start_capital_still_converts():
    text = "\u0414\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430"
    assert textclean.apply(text, ON_RU)[0] == (
        "23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430")
    assert textclean.apply("\u041f\u0435\u0440\u0432\u043e\u0433\u043e \u044f\u043d\u0432\u0430\u0440\u044f", ON_RU)[0] == "01.01"


@needs_wetext
def test_english_sentence_start_and_month_case():
    assert textclean.apply("Twenty three", ON_EN)[0] == "23"
    assert textclean.apply("March twenty third", ON_EN)[0] == "March 23rd"
    assert textclean.apply("The twenty third of March", ON_EN)[0] == (
        "The 23rd of March")


@needs_wetext
def test_the_word_next_to_a_number_keeps_its_spelling():
    text = "\u0441\u0442\u043e \u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f"
    assert textclean.apply(text, ON_RU)[0] == "123 \u0440\u0443\u0431\u043b\u044f"


def test_a_bare_small_number_is_left_to_wetext():
    # wetext converts a numeral only in a recognized phrase (a date, a time,
    # a measure); a lone "\u043e\u0434\u0438\u043d" may be an article, so it stays as spoken.
    text = "\u042f \u043a\u0443\u043f\u0438\u043b \u043e\u0434\u0438\u043d \u0431\u0438\u043b\u0435\u0442."
    assert textclean.apply(text, ON_RU)[0] == text


def test_another_language_leaves_the_numbers_alone():
    text = "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438"
    assert textclean.apply(text, {"text_cleanup": True, "language": "de"})[0] == (
        "\u0414\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438")


def test_the_normalizer_loads_lazily_and_once(monkeypatch, fresh_normalizers):
    loads = []

    class _Stub:
        def normalize(self, text):
            return "23"

    def load(lang):
        loads.append(lang)
        return _Stub()

    monkeypatch.setattr(textclean, "_load_normalizer", load)
    assert textclean.apply("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438", ON_RU)[0] == "23"
    assert textclean.apply("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438", ON_RU)[0] == "23"
    assert loads == ["ru"]


def test_no_number_means_no_library_load(monkeypatch, fresh_normalizers):
    loads = []
    monkeypatch.setattr(
        textclean, "_load_normalizer",
        lambda lang: loads.append(lang) or None)
    assert textclean.apply("\u043f\u0440\u0438\u0432\u0435\u0442, \u044d\u044d\u044d", ON_RU)[0] == (
        "\u041f\u0440\u0438\u0432\u0435\u0442")
    assert loads == []


def test_a_wetext_failure_keeps_the_words_and_the_dictation(
        monkeypatch, fresh_normalizers):
    def fail(_lang):
        raise RuntimeError("wetext is unavailable")

    monkeypatch.setattr(textclean, "_load_normalizer", fail)
    text = "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430"
    assert textclean.apply(text, ON_RU) == (
        "\u0414\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438, \u0437\u0430\u0432\u0442\u0440\u0430", 2)


# -- filler words -----------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("\u041d\u0443, \u044f \u043f\u0440\u0438\u0448\u0451\u043b, \u043a\u043e\u0440\u043e\u0447\u0435.",
     "\u042f \u043f\u0440\u0438\u0448\u0451\u043b."),
    ("\u042d\u0442\u043e, \u0442\u0438\u043f\u0430, \u0442\u0435\u0441\u0442.", "\u042d\u0442\u043e \u0442\u0435\u0441\u0442."),
    ("\u044d\u044d\u044d \u044d\u0442\u043e \u044d\u043c \u0442\u0435\u0441\u0442", "\u042d\u0442\u043e \u0442\u0435\u0441\u0442"),
])
def test_russian_fillers_are_dropped(text, expected):
    assert textclean.apply(text, ON_RU)[0] == expected


def test_russian_fillers_never_touch_a_word():
    text = "\u042d\u0442\u043e \u043d\u0443\u0436\u0434\u044b, \u0430 \u043d\u0435 \u044d\u043c\u043f\u0430\u0442\u0438\u044f."
    assert textclean.apply(text, ON_RU)[0] == text


def test_a_filler_word_keeps_its_meaning_without_a_setting_off_mark():
    text = "\u0432\u043e\u0442 \u0434\u043e\u043c. \u041d\u0443\u0436\u043d\u043e \u044d\u043c\u043f\u0430\u0442\u0438\u0438 \u043a\u043e\u0440\u043e\u0447\u0435 \u043d\u0435 \u0431\u044b\u0432\u0430\u0435\u0442."
    assert textclean.apply(text, ON_RU)[0] == (
        "\u0412\u043e\u0442 \u0434\u043e\u043c. \u041d\u0443\u0436\u043d\u043e \u044d\u043c\u043f\u0430\u0442\u0438\u0438 "
        "\u043a\u043e\u0440\u043e\u0447\u0435 \u043d\u0435 \u0431\u044b\u0432\u0430\u0435\u0442.")


def test_english_fillers_are_dropped():
    text = "It's, like, five, you know."
    assert textclean.apply(text, ON_EN)[0] == "It's five."


def test_english_like_inside_a_sentence_keeps_its_meaning():
    text = "I like it"
    assert textclean.apply(text, ON_EN)[0] == text


# -- capitals ---------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("\u043f\u0440\u0438\u0432\u0435\u0442. \u043a\u0430\u043a \u0434\u0435\u043b\u0430? \u0445\u043e\u0440\u043e\u0448\u043e!\n\u0435\u0449\u0451",
     "\u041f\u0440\u0438\u0432\u0435\u0442. \u041a\u0430\u043a \u0434\u0435\u043b\u0430? \u0425\u043e\u0440\u043e\u0448\u043e!\n\u0415\u0449\u0451"),
    ("\u0441\u0442\u043e\u043f\u2026 \u0434\u0430\u043b\u044c\u0448\u0435",
     "\u0421\u0442\u043e\u043f\u2026 \u0414\u0430\u043b\u044c\u0448\u0435"),
    ("23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430", "23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430"),
])
def test_sentences_start_with_a_capital(text, expected):
    assert textclean.apply(text, {"text_cleanup": True})[0] == expected


def test_a_capital_after_a_protected_span_still_works():
    text = "\u0421\u043c\u043e\u0442\u0440\u0438 `file_name.py`. \u0433\u043e\u0442\u043e\u0432\u043e"
    assert textclean.apply(text, ON_RU)[0] == (
        "\u0421\u043c\u043e\u0442\u0440\u0438 `file_name.py`. \u0413\u043e\u0442\u043e\u0432\u043e")


# -- protection -------------------------------------------------------------


PROTECTED_TEXT = (
    "file_name.py app.textclean.apply https://example.com/twenty-three "
    "user@example.com textCleanup `\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438, \u044d\u044d\u044d`"
)


def test_protected_spans_survive_unchanged():
    assert textclean.apply(PROTECTED_TEXT, ON_RU) == (PROTECTED_TEXT, 0)


def test_a_windows_path_survives_unchanged():
    text = "C:\\Users\\me\\file.py"
    assert textclean.apply(text, ON_RU) == (text, 0)


def test_a_filler_next_to_a_protected_span_is_dropped_and_the_span_stays():
    text = "`file_name.py`, \u044d\u044d\u044d, \u0433\u043e\u0442\u043e\u0432\u043e"
    assert textclean.apply(text, ON_RU)[0] == "`file_name.py`, \u0433\u043e\u0442\u043e\u0432\u043e"


# -- the service pipeline ---------------------------------------------------


def test_service_keeps_the_text_when_cleanup_is_off():
    svc = make_service()
    svc.asr = _ASR("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == (
        "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430")


@needs_wetext
def test_the_panel_draft_receives_the_cleaned_text():
    svc = make_service(text_cleanup=True)
    svc.asr = _ASR("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430"


@needs_wetext
def test_cleanup_runs_after_a_snippet_expands():
    svc = make_service(text_cleanup=True, snippets=[
        {"trigger": "send the report",
         "text": "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430"}])
    svc.asr = _ASR("send the report")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430"


@needs_wetext
def test_insertion_receives_the_cleaned_text(monkeypatch):
    captured = []
    fake = types.ModuleType("insert")
    fake.RESULT_PASTED = "pasted"
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"

    def insert_text(text, **_kwargs):
        captured.append(text)
        return fake.RESULT_PASTED

    fake.insert_text = insert_text
    monkeypatch.setitem(sys.modules, "insert", fake)
    svc = make_service(text_cleanup=True, confirm_focus_change=False)
    svc.asr = _ASR("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430")
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert captured == ["23 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430"]


def test_a_phrase_of_fillers_only_reaches_no_field(monkeypatch):
    captured = []
    fake = types.ModuleType("insert")
    fake.RESULT_PASTED = "pasted"
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.insert_text = lambda text, **_kwargs: captured.append(text)
    monkeypatch.setitem(sys.modules, "insert", fake)
    svc = make_service(text_cleanup=True, confirm_focus_change=False)
    svc.asr = _ASR("\u044d\u044d\u044d")
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert captured == []
    assert svc.live["last"] == svc.tr("notify.not_recognized")


def test_a_wetext_failure_still_inserts_the_rest(
        monkeypatch, fresh_normalizers):
    def fail(_lang):
        raise RuntimeError("wetext is unavailable")

    monkeypatch.setattr(textclean, "_load_normalizer", fail)
    svc = make_service(text_cleanup=True)
    svc.asr = _ASR("\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u044d\u044d\u044d, \u0437\u0430\u0432\u0442\u0440\u0430")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == (
        "\u0414\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438 \u0440\u0443\u0431\u043b\u044f, \u0437\u0430\u0432\u0442\u0440\u0430")


def test_cleanup_runs_after_the_snippets_and_before_the_draft():
    source = SERVICE.read_text(encoding="utf-8")
    snippet_at = source.index("snippets.apply(")
    cleanup_at = source.index("textclean.apply(")
    draft_at = source.index("self._show_draft(")
    insert_at = source.index("insert_mod.insert_text(")
    assert snippet_at < cleanup_at < draft_at < insert_at
