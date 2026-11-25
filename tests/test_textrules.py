"""Text rules: backtrack, spoken lists and the opt-in pipeline (card-188)."""
import json
import sys
import types
from pathlib import Path

import config
import pytest
import textrules
from vibeforma.service import Service

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app" / "vibeforma" / "service.py"
SETTINGS = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"
LANGS = ("en", "ru")


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
        "dictionary": [],
        "text_rules": False,
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
    }
    base.update(cfg)
    svc = Service(base, _Log())
    svc._running = True
    return svc


@pytest.mark.parametrize("text, expected", [
    ("\u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c."),
    ("\u0421\u043e\u0437\u0432\u043e\u043d \u0432 \u0448\u0435\u0441\u0442\u044c \u043d\u0435\u0442 \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c."),
    ("\u041e\u0442\u043f\u0440\u0430\u0432\u044c \u043e\u0442\u0447\u0451\u0442 \u0432\u0447\u0435\u0440\u0430, \u0442\u043e \u0435\u0441\u0442\u044c \u043d\u0435\u0442, \u0437\u0430\u0432\u0442\u0440\u0430.", "\u0417\u0430\u0432\u0442\u0440\u0430."),
    ("\u0421\u043e\u0437\u0432\u043e\u043d \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u043e\u043e\u0431\u0449\u0435-\u0442\u043e \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c."),
    ("\u041f\u043e\u0437\u0432\u043e\u043d\u0438 \u043c\u0430\u043c\u0435, \u043f\u0440\u043e\u0441\u0442\u0438, \u044f \u0438\u043c\u0435\u043b \u0432 \u0432\u0438\u0434\u0443 \u043f\u0430\u043f\u0435.", "\u041f\u0430\u043f\u0435."),
    ("\u041f\u043e\u0437\u0432\u043e\u043d\u0438 \u043c\u0430\u043c\u0435 \u043f\u0440\u043e\u0441\u0442\u0438 \u044f \u0438\u043c\u0435\u043b \u0432 \u0432\u0438\u0434\u0443 \u043f\u0430\u043f\u0435.", "\u041f\u0430\u043f\u0435."),
    ("\u0421\u043e\u0437\u0432\u043e\u043d \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c."),
])
def test_backtrack_replaces_the_thought_after_each_trigger(text, expected):
    assert textrules.backtrack(text) == expected


def test_backtrack_keeps_earlier_sentences():
    text = "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c."
    assert textrules.backtrack(text) == "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412 \u0441\u0435\u043c\u044c."


def test_backtrack_corrects_the_previous_sentence_when_the_trigger_opens_one():
    text = "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c. \u041d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c."
    assert textrules.backtrack(text) == "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412 \u0441\u0435\u043c\u044c."


def test_backtrack_drops_the_thought_without_a_new_wording():
    assert textrules.backtrack(
        "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435.") == "\u041f\u0440\u0438\u0432\u0435\u0442."
    assert textrules.backtrack("\u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435.") == ""


@pytest.mark.parametrize("text", [
    "\u041d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.",
    "\u2026, \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.",
    "\u042d\u0442\u043e \u0442\u043e\u0447\u043d\u0435\u0435, \u0447\u0435\u043c \u0432\u0447\u0435\u0440\u0430.",
    "\u041c\u044b \u043f\u0440\u043e\u0441\u0442\u043e \u0443\u0442\u043e\u0447\u043d\u0438\u043c \u0432\u0440\u0435\u043c\u044f.",
])
def test_backtrack_needs_a_thought_before_the_trigger(text):
    assert textrules.backtrack(text) == text


@pytest.mark.parametrize("text, expected", [
    ("\u041a\u0443\u043f\u0438: \u043e\u0434\u0438\u043d, \u0445\u043b\u0435\u0431, \u0434\u0432\u0430, \u043c\u043e\u043b\u043e\u043a\u043e.",
     "\u041a\u0443\u043f\u0438:\n\n1. \u0445\u043b\u0435\u0431\n2. \u043c\u043e\u043b\u043e\u043a\u043e"),
    ("\u041f\u0435\u0440\u0432\u043e\u0435, \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431, \u0432\u0442\u043e\u0440\u043e\u0435, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435.",
     "1. \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431\n2. \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435"),
    ("\u0412\u043e-\u043f\u0435\u0440\u0432\u044b\u0445, \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431, \u0432\u043e-\u0432\u0442\u043e\u0440\u044b\u0445, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435.",
     "1. \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431\n2. \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435"),
    ("\u041f\u0443\u043d\u043a\u0442 \u043e\u0434\u0438\u043d: \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431, \u043f\u0443\u043d\u043a\u0442 \u0434\u0432\u0430: \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435.",
     "1. \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431\n2. \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435"),
    ("\u041e\u0434\u0438\u043d, \u0440\u0430\u0437, \u0434\u0432\u0430, \u0434\u0432\u0430\u0436\u0434\u044b, \u0442\u0440\u0438, \u0442\u0440\u0438\u0436\u0434\u044b.",
     "1. \u0440\u0430\u0437\n2. \u0434\u0432\u0430\u0436\u0434\u044b\n3. \u0442\u0440\u0438\u0436\u0434\u044b"),
    ("\u041a\u0443\u043f\u0438: \u043e\u0434\u0438\u043d, \u0445\u043b\u0435\u0431, \u0434\u0432\u0430, \u043c\u043e\u043b\u043e\u043a\u043e. \u0418 \u043f\u043e\u0437\u0432\u043e\u043d\u0438 \u043c\u0430\u043c\u0435.",
     "\u041a\u0443\u043f\u0438:\n\n1. \u0445\u043b\u0435\u0431\n2. \u043c\u043e\u043b\u043e\u043a\u043e\n\n\u0418 \u043f\u043e\u0437\u0432\u043e\u043d\u0438 \u043c\u0430\u043c\u0435."),
])
def test_lists_turn_a_spoken_row_into_a_numbered_list(text, expected):
    assert textrules.lists(text) == expected


@pytest.mark.parametrize("text", [
    "\u042f \u043a\u0443\u043f\u0438\u043b \u043e\u0434\u0438\u043d \u0431\u0438\u043b\u0435\u0442 \u0438 \u0434\u0432\u0430 \u0436\u0443\u0440\u043d\u0430\u043b\u0430.",
    "\u041e\u0434\u0438\u043d \u043c\u043e\u0439 \u0434\u0440\u0443\u0433, \u0434\u0432\u0430 \u0435\u0433\u043e \u0442\u043e\u0432\u0430\u0440\u0438\u0449\u0430 \u043f\u0440\u0438\u0448\u043b\u0438.",
    "\u041f\u0435\u0440\u0432\u043e\u0435 \u0432\u043f\u0435\u0447\u0430\u0442\u043b\u0435\u043d\u0438\u0435 \u0431\u044b\u043b\u043e \u0441\u0438\u043b\u044c\u043d\u044b\u043c, \u0432\u0442\u043e\u0440\u043e\u0435 \u2014 \u0441\u043b\u0430\u0431\u044b\u043c.",
    "\u0412\u043e-\u043f\u0435\u0440\u0432\u044b\u0445, \u044f \u043d\u0435 \u0433\u043e\u043b\u043e\u0434\u0435\u043d.",
    "\u041f\u0443\u043d\u043a\u0442 \u043e\u0434\u0438\u043d: \u0441\u0434\u0435\u043b\u0430\u0442\u044c.",
    "\u041e\u0434\u0438\u043d, \u0434\u0432\u0430, \u0442\u0440\u0438.",
    "\u0414\u0432\u0430, \u0445\u043b\u0435\u0431, \u043e\u0434\u0438\u043d, \u043c\u043e\u043b\u043e\u043a\u043e.",
])
def test_lists_need_a_full_row_of_markers(text):
    assert textrules.lists(text) == text


@pytest.mark.parametrize("cfg", [{}, {"text_rules": False}, None])
def test_apply_is_off_by_default(cfg):
    text = "\u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c."
    assert textrules.apply(text, cfg) == (text, 0)


def test_apply_runs_both_rules_and_counts_them():
    text = ("\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c. "
            "\u041a\u0443\u043f\u0438: \u043e\u0434\u0438\u043d, \u0445\u043b\u0435\u0431, \u0434\u0432\u0430, \u043c\u043e\u043b\u043e\u043a\u043e.")
    result, count = textrules.apply(text, {"text_rules": True})
    assert result == "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412 \u0441\u0435\u043c\u044c. \u041a\u0443\u043f\u0438:\n\n1. \u0445\u043b\u0435\u0431\n2. \u043c\u043e\u043b\u043e\u043a\u043e"
    assert count == 2


def test_apply_reports_zero_when_nothing_fires():
    assert textrules.apply("\u041e\u0431\u044b\u0447\u043d\u0430\u044f \u0444\u0440\u0430\u0437\u0430.", {"text_rules": True}) == (
        "\u041e\u0431\u044b\u0447\u043d\u0430\u044f \u0444\u0440\u0430\u0437\u0430.", 0)


def test_text_rules_default_is_off():
    assert config.DEFAULTS["text_rules"] is False


def test_locale_entries_for_the_toggle():
    keys = {"settings.g_text_rules", "settings.text_rules",
            "settings.note_text_rules"}
    for lang in LANGS:
        with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
            strings = json.load(handle)
        assert keys <= set(strings)
        assert all(str(strings[key]).strip() for key in keys)


def test_settings_toggle_is_wired_on_the_record_tab():
    source = SETTINGS.read_text(encoding="utf-8")
    record_tab = source[source.index("def _build_record_tab"):
                        source.index("def _build_model_tab")]
    assert "v_text_rules" in record_tab
    assert "settings.note_text_rules" in record_tab
    save = source[source.index("def _save(self):"):]
    assert 'c["text_rules"] = bool(self.v_text_rules.get())' in save


def test_pipeline_order_in_the_service_source():
    source = SERVICE.read_text(encoding="utf-8")
    dictionary_at = source.index("dictionary.apply(")
    rules_at = source.index("textrules.apply(")
    snippet_at = source.index("snippets (card-181)")
    draft_at = source.index("self._show_draft(")
    insert_at = source.index("insert_mod.insert_text(")
    assert dictionary_at < rules_at < snippet_at < draft_at < insert_at
    assert rules_at < source.index("if not text:", rules_at)


def test_dictionary_runs_before_text_rules():
    svc = make_service(
        text_rules=True,
        dictionary=[{"from": "\u0448\u0435\u0441\u0442\u044c", "to": "\u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0441\u0435\u043c\u044c"}])
    svc.asr = _ASR("\u0432\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "\u0421\u0435\u043c\u044c"


def test_service_keeps_the_text_when_text_rules_are_off():
    svc = make_service(
        dictionary=[{"from": "\u0448\u0435\u0441\u0442\u044c", "to": "\u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0441\u0435\u043c\u044c"}])
    svc.asr = _ASR("\u0432\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "\u0432\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0441\u0435\u043c\u044c"


def test_panel_draft_receives_the_processed_text():
    svc = make_service(text_rules=True)
    svc.asr = _ASR("\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412 \u0441\u0435\u043c\u044c."


def test_insertion_receives_the_processed_text(monkeypatch):
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
    svc = make_service(text_rules=True, confirm_focus_change=False)
    svc.asr = _ASR("\u041a\u0443\u043f\u0438: \u043e\u0434\u0438\u043d, \u0445\u043b\u0435\u0431, \u0434\u0432\u0430, \u043c\u043e\u043b\u043e\u043a\u043e.")
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert captured == ["\u041a\u0443\u043f\u0438:\n\n1. \u0445\u043b\u0435\u0431\n2. \u043c\u043e\u043b\u043e\u043a\u043e"]


CORPUS = [
    ("\u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c.", 1),
    ("\u041e\u0442\u043f\u0440\u0430\u0432\u044c \u043e\u0442\u0447\u0451\u0442 \u0432\u0447\u0435\u0440\u0430, \u0442\u043e \u0435\u0441\u0442\u044c \u043d\u0435\u0442, \u0437\u0430\u0432\u0442\u0440\u0430.", "\u0417\u0430\u0432\u0442\u0440\u0430.", 1),
    ("\u0421\u043e\u0437\u0432\u043e\u043d \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u043e\u043e\u0431\u0449\u0435-\u0442\u043e \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c.", 1),
    ("\u041f\u043e\u0437\u0432\u043e\u043d\u0438 \u043c\u0430\u043c\u0435, \u043f\u0440\u043e\u0441\u0442\u0438, \u044f \u0438\u043c\u0435\u043b \u0432 \u0432\u0438\u0434\u0443 \u043f\u0430\u043f\u0435.", "\u041f\u0430\u043f\u0435.", 1),
    ("\u0421\u043e\u0437\u0432\u043e\u043d \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", "\u0412 \u0441\u0435\u043c\u044c.", 1),
    ("\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u0432\u0435\u0440\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412 \u0441\u0435\u043c\u044c.", 1),
    ("\u041f\u0440\u0438\u0432\u0435\u0442. \u0412\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c. \u041d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.",
     "\u041f\u0440\u0438\u0432\u0435\u0442. \u0412 \u0441\u0435\u043c\u044c.", 1),
    ("\u041a\u0443\u043f\u0438: \u043e\u0434\u0438\u043d, \u0445\u043b\u0435\u0431, \u0434\u0432\u0430, \u043c\u043e\u043b\u043e\u043a\u043e.",
     "\u041a\u0443\u043f\u0438:\n\n1. \u0445\u043b\u0435\u0431\n2. \u043c\u043e\u043b\u043e\u043a\u043e", 1),
    ("\u041f\u0435\u0440\u0432\u043e\u0435, \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431, \u0432\u0442\u043e\u0440\u043e\u0435, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435.",
     "1. \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431\n2. \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435", 1),
    ("\u0412\u043e-\u043f\u0435\u0440\u0432\u044b\u0445, \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431, \u0432\u043e-\u0432\u0442\u043e\u0440\u044b\u0445, \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435.",
     "1. \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431\n2. \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435", 1),
    ("\u041f\u0443\u043d\u043a\u0442 \u043e\u0434\u0438\u043d: \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431, \u043f\u0443\u043d\u043a\u0442 \u0434\u0432\u0430: \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435.",
     "1. \u043a\u0443\u043f\u0438\u0442\u044c \u0445\u043b\u0435\u0431\n2. \u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c \u043c\u0430\u043c\u0435", 1),
    ("\u041e\u0434\u0438\u043d, \u0440\u0430\u0437, \u0434\u0432\u0430, \u0434\u0432\u0430\u0436\u0434\u044b, \u0442\u0440\u0438, \u0442\u0440\u0438\u0436\u0434\u044b.",
     "1. \u0440\u0430\u0437\n2. \u0434\u0432\u0430\u0436\u0434\u044b\n3. \u0442\u0440\u0438\u0436\u0434\u044b", 1),
    ("\u041a\u0443\u043f\u0438 \u043e\u0434\u0438\u043d \u0445\u043b\u0435\u0431 \u0434\u0432\u0430 \u043c\u043e\u043b\u043e\u043a\u043e.", "1. \u0445\u043b\u0435\u0431\n2. \u043c\u043e\u043b\u043e\u043a\u043e", 1),
    ("\u042f \u043a\u0443\u043f\u0438\u043b \u043e\u0434\u0438\u043d \u0431\u0438\u043b\u0435\u0442 \u0438 \u0434\u0432\u0430 \u0436\u0443\u0440\u043d\u0430\u043b\u0430.", None, 0),
    ("\u041e\u0434\u0438\u043d \u043c\u043e\u0439 \u0434\u0440\u0443\u0433, \u0434\u0432\u0430 \u0435\u0433\u043e \u0442\u043e\u0432\u0430\u0440\u0438\u0449\u0430 \u043f\u0440\u0438\u0448\u043b\u0438.", None, 0),
    ("\u041f\u0435\u0440\u0432\u043e\u0435 \u0432\u043f\u0435\u0447\u0430\u0442\u043b\u0435\u043d\u0438\u0435 \u0431\u044b\u043b\u043e \u0441\u0438\u043b\u044c\u043d\u044b\u043c, \u0432\u0442\u043e\u0440\u043e\u0435 \u2014 \u0441\u043b\u0430\u0431\u044b\u043c.", None, 0),
    ("\u041d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435 \u0432 \u0441\u0435\u043c\u044c.", None, 0),
    ("\u042d\u0442\u043e \u0442\u043e\u0447\u043d\u0435\u0435, \u0447\u0435\u043c \u0432\u0447\u0435\u0440\u0430.", None, 0),
    ("\u041d\u0430\u0432\u0435\u0440\u043d\u043e\u0435, \u043f\u043e\u0439\u0434\u0451\u0442 \u0434\u043e\u0436\u0434\u044c.", None, 0),
    ("\u0412\u043e-\u043f\u0435\u0440\u0432\u044b\u0445, \u044f \u043d\u0435 \u0433\u043e\u043b\u043e\u0434\u0435\u043d.", None, 0),
    ("\u041f\u0443\u043d\u043a\u0442 \u043e\u0434\u0438\u043d: \u0441\u0434\u0435\u043b\u0430\u0442\u044c.", None, 0),
    ("\u041e\u0434\u0438\u043d, \u0434\u0432\u0430, \u0442\u0440\u0438.", None, 0),
]


@pytest.fixture(scope="module")
def corpus():
    return list(CORPUS)


def corpus_metrics(corpus):
    tp = fp = fn = 0
    for text, expected, hits in corpus:
        result, count = textrules.apply(text, {"text_rules": True})
        if hits:
            if count >= 1 and result == expected:
                tp += 1
            else:
                fn += 1
        elif count or result != text:
            fp += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall}


def test_corpus_has_positive_and_negative_samples(corpus):
    assert any(hits for _text, _expected, hits in corpus)
    assert any(not hits for _text, _expected, hits in corpus)


def test_corpus_precision_and_recall(corpus):
    metrics = corpus_metrics(corpus)
    assert metrics["precision"] >= 0.9
    assert metrics["recall"] >= 0.8


def test_cleanup_runs_after_the_rules_and_the_snippets():
    source = SERVICE.read_text(encoding="utf-8")
    rules_at = source.index("textrules.apply(")
    snippet_at = source.index("snippets.apply(")
    cleanup_at = source.index("textclean.apply(")
    draft_at = source.index("self._show_draft(")
    insert_at = source.index("insert_mod.insert_text(")
    assert rules_at < snippet_at < cleanup_at < draft_at < insert_at


def test_cleanup_finishes_the_rules_output():
    svc = make_service(text_rules=True, text_cleanup=True, language="ru")
    svc.asr = _ASR("\u0432\u0441\u0442\u0440\u0435\u0447\u0430\u0435\u043c\u0441\u044f \u0432 \u0448\u0435\u0441\u0442\u044c, \u043d\u0435\u0442, \u0442\u043e\u0447\u043d\u0435\u0435, \u044d\u044d\u044d, \u0432 \u0441\u0435\u043c\u044c")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "\u0412 \u0441\u0435\u043c\u044c"
