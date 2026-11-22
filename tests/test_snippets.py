"""Snippets: trigger expansion, validation, JSON exchange and the pipeline."""
import json
import sys
import types
from pathlib import Path

import config
import pytest
import snippets
from vibeforma.service import Service

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app" / "vibeforma" / "service.py"
SETTINGS = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"
LANGS = ("en", "ru")


# -- replacement -----------------------------------------------------------
def test_trigger_replacement_ignores_case():
    entries = [{"trigger": "send the report", "text": "Quarterly numbers"}]
    text, count = snippets.apply("please SEND THE REPORT today", entries)
    assert text == "please Quarterly numbers today"
    assert count == 1


def test_trigger_respects_word_boundaries():
    entries = [{"trigger": "consider", "text": "X"}]
    text, count = snippets.apply("reconsider this", entries)
    assert text == "reconsider this"
    assert count == 0


def test_multiword_trigger_is_replaced_as_a_whole():
    entries = [{"trigger": "new york", "text": "NY"}]
    text, count = snippets.apply("I live in New York.", entries)
    assert text == "I live in NY."
    assert count == 1


def test_multiline_text_keeps_its_line_breaks():
    entries = [{"trigger": "my signature", "text": "Best regards,\nAlice"}]
    text, count = snippets.apply("my signature", entries)
    assert text == "Best regards,\nAlice"
    assert count == 1


def test_longest_trigger_wins():
    entries = [{"trigger": "new york", "text": "NY"},
               {"trigger": "york", "text": "Y"}]
    text, count = snippets.apply("new york and york", entries)
    assert text == "NY and Y"
    assert count == 2


def test_replacements_are_not_reprocessed():
    entries = [{"trigger": "foo", "text": "bar"},
               {"trigger": "bar", "text": "baz"}]
    text, _count = snippets.apply("foo and bar", entries)
    assert text == "bar and baz"


def test_empty_config_keeps_the_text():
    assert snippets.apply("hello", None) == ("hello", 0)
    assert snippets.apply("hello", []) == ("hello", 0)
    assert snippets.normalize(None) == []


# -- normalize -------------------------------------------------------------
def test_normalize_drops_duplicates_and_invalid_items():
    entries = snippets.normalize([
        {"trigger": "cat", "text": "dog"},
        {"trigger": "CAT", "text": "mouse"},
        {"trigger": "", "text": "x"},
        {"trigger": "same", "text": "same"},
        {"trigger": "no text", "text": ""},
        "not a pair",
    ])
    assert entries == [{"trigger": "cat", "text": "dog"}]


def test_normalize_caps_trigger_and_text_length():
    entries = snippets.normalize([
        {"trigger": "a" * 100, "text": "t" * 3000}])
    assert entries[0]["trigger"] == "a" * snippets.MAX_TRIGGER
    assert entries[0]["text"] == "t" * snippets.MAX_TEXT


def test_normalize_caps_the_pair_list():
    raw = [{"trigger": f"phrase {i:03d}", "text": "x"} for i in range(510)]
    assert len(snippets.normalize(raw)) == snippets.MAX_ENTRIES


# -- validation ------------------------------------------------------------
def test_validate_reports_a_repeated_trigger():
    issues = snippets.validate([
        {"trigger": "cat", "text": "dog"},
        {"trigger": "CAT", "text": "mouse"},
    ])
    assert [(issue["level"], issue["code"]) for issue in issues] == [
        ("error", "duplicate")]


def test_validate_reports_a_dictionary_collision():
    issues = snippets.validate(
        [{"trigger": "vibeforma", "text": "the product"}],
        [{"from": "VibeForma", "to": "VibeForma app"}])
    assert [(issue["level"], issue["code"]) for issue in issues] == [
        ("error", "dictionary")]


def test_validate_warns_about_a_one_word_common_trigger():
    issues = snippets.validate([{"trigger": "the", "text": "article"}])
    assert [(issue["level"], issue["code"]) for issue in issues] == [
        ("warning", "frequent")]


def test_validate_warns_about_a_russian_common_word():
    issues = snippets.validate(
        [{"trigger": "\u043d\u0435\u0442", "text": "no"}])
    assert [issue["code"] for issue in issues] == ["frequent"]


def test_validate_accepts_a_multiword_trigger_with_common_words():
    assert snippets.validate(
        [{"trigger": "the report", "text": "quarterly"}]) == []


# -- JSON ------------------------------------------------------------------
def test_json_roundtrip():
    entries = [{"trigger": "my address", "text": "Line 1\nLine 2"}]
    assert snippets.from_json(snippets.to_json(entries)) == entries


def test_json_accepts_wrapped_objects_and_pairs():
    wrapped = '{"snippets": [{"trigger": "a b", "text": "c"}]}'
    assert snippets.from_json(wrapped) == [{"trigger": "a b", "text": "c"}]
    assert snippets.from_json('[["a b", "c"]]') == [
        {"trigger": "a b", "text": "c"}]


def test_json_rejects_malformed_payloads():
    for raw in ("not json", '{"snippets": 1}', '"text"'):
        with pytest.raises(ValueError):
            snippets.from_json(raw)


def test_json_of_an_empty_list_is_readable():
    assert json.loads(snippets.to_json([])) == []


# -- config, locales and the tab wiring ------------------------------------
def test_config_default_is_an_empty_list():
    assert config.DEFAULTS["snippets"] == []


def test_locale_entries_for_the_tab():
    keys = {"tabs.snippets", "settings.sub_snippets", "snippets.g",
            "snippets.note", "snippets.trigger", "snippets.text",
            "snippets.add", "snippets.update", "snippets.remove",
            "snippets.import", "snippets.export", "snippets.preview",
            "snippets.preview_result", "snippets.preview_same",
            "snippets.preview_none", "snippets.issue_duplicate",
            "snippets.issue_dictionary", "snippets.issue_frequent"}
    for lang in LANGS:
        with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
            strings = json.load(handle)
        assert keys <= set(strings), lang
        assert all(str(strings[key]).strip() for key in keys)


def test_settings_window_wires_the_tab():
    source = SETTINGS.read_text(encoding="utf-8")
    assert '("snippets", "tabs.snippets")' in source
    assert "snippets_ui.build_snippets_tab(" in source
    save = source[source.index("def _save(self):"):]
    assert 'c["snippets"] = snippets.normalize(self.snippets)' in save
    reset = source[source.index("def _reset(self):"):
                   source.index("def _poll_model(")]
    assert "self.snippets_tab.reset()" in reset


def test_snippets_ui_builds_the_tab_and_preview():
    import snippets_ui

    source = Path(snippets_ui.__file__).read_text(encoding="utf-8")
    for name in ("build_snippets_tab", "def _preview", "def _import",
                 "def _export", "def _refresh"):
        assert name in source


# -- the pipeline ----------------------------------------------------------
class RecordingLog:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def exception(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def debug(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)


class FakeRecorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class FakeASR:
    def __init__(self, text):
        self._text = text

    def transcribe(self, _buf):
        return self._text


def make_service(**overrides):
    cfg = {
        "locale": "en",
        "dictionary": [],
        "snippets": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
        "confirm_focus_change": False,
    }
    cfg.update(overrides)
    svc = Service(cfg, RecordingLog())
    svc._running = True
    return svc


def test_pipeline_order_in_the_service_source():
    source = SERVICE.read_text(encoding="utf-8")
    dictionary_at = source.index("dictionary.apply(")
    rules_at = source.index("textrules.apply(")
    snippet_at = source.index("snippets.apply(")
    draft_at = source.index("self._show_draft(")
    insert_at = source.index("insert_mod.insert_text(")
    assert dictionary_at < rules_at < snippet_at < draft_at < insert_at


def test_panel_draft_receives_the_expanded_text():
    svc = make_service(snippets=[
        {"trigger": "send the report", "text": "Line one\nLine two"}])
    svc.asr = FakeASR("send the report")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.live["draft"] == "Line one\nLine two"


def test_insertion_receives_the_expanded_text(monkeypatch):
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
    svc = make_service(snippets=[
        {"trigger": "send the report", "text": "Line one\nLine two"}])
    svc.asr = FakeASR("send the report")
    svc.state = "processing"

    svc._transcribe_worker(object(), FakeRecorder())

    assert captured == ["Line one\nLine two"]


def test_dictionary_runs_before_snippets():
    svc = make_service(
        dictionary=[{"from": "helo wrld", "to": "hello world"}],
        snippets=[{"trigger": "hello world", "text": "EXPANDED"}])
    svc.asr = FakeASR("helo wrld")
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.live["draft"] == "EXPANDED"
