"""The Sound source switch: the Record tab group, save, reset and locales."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

KEYS = (
    "settings.g_source",
    "settings.source",
    "settings.source_mic",
    "settings.source_system",
    "settings.note_source",
)


def _source():
    return SOURCE.read_text(encoding="utf-8")


def _method(text, name):
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


def test_source_group_is_built_on_the_record_tab():
    record = _method(_source(), "_build_record_tab")
    assert "settings.g_source" in record
    assert "self.v_source" in record
    assert "settings.note_source" in record


def test_source_maps_labels_to_codes():
    record = _method(_source(), "_build_record_tab")
    assert "self._source_by_name" in record
    assert '"microphone"' in record
    assert '"system"' in record


def test_source_is_saved_and_reset():
    text = _source()
    save = _method(text, "_save")
    assert 'c["audio_source"] = self._source_by_name.get(' in save
    reset = _method(text, "_reset")
    assert "self.v_source.set(" in reset


def test_source_labels_are_localized():
    for lang in ("en", "ru"):
        path = LOCALES / (lang + ".json")
        with open(path, encoding="utf-8") as handle:
            catalog = json.load(handle)
        for key in KEYS:
            assert str(catalog.get(key, "")).strip(), (lang, key)
