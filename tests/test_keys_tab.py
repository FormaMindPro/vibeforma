"""The Hotkeys settings tab: registration, source wiring and locale keys."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

KEYS = (
    "keys.esc_cancel",
    "keys.esc_panel",
    "keys.ctrl_enter",
    "keys.paste_shift_insert",
    "keys.paste_ctrl_v",
    "keys.ctrl_f",
    "keys.ctrl_s",
    "keys.esc_settings",
    "keys.delete_history",
    "keys.ctrl_a_history",
    "keys.fixed_note",
)

COMBOS = ("Esc", "Ctrl+Enter", "Shift+Insert", "Ctrl+V", "Ctrl+F", "Ctrl+S",
          "Delete", "Ctrl+A")


def _source():
    return SOURCE.read_text(encoding="utf-8")


def _method(text, name):
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


def test_keys_tab_is_registered_in_the_panes():
    panes = _method(_source(), "_create_panes")
    assert '("keys", "tabs.keys")' in panes
    assert '("keys", None)' in panes


def test_keys_tab_is_built_in_init():
    text = _source()
    init = text[text.index("class SettingsWindow"):]
    init = init[:init.index("\n    def ", init.index("def __init__"))]
    assert '_build_keys_tab(frames["keys"])' in init


def test_keys_frame_is_in_the_fit_tuple():
    fit = _method(_source(), "_fit_to_content")
    assert '"keys"' in fit


def test_hotkey_and_hold_live_on_the_keys_tab():
    text = _source()
    keys = _method(text, "_build_keys_tab")
    assert "self.v_hotkey" in keys
    assert "self.v_hold" in keys
    assert "settings.note_hotkey" in keys
    assert "settings.note_hold_to_talk" in keys
    record = text[text.index("def _build_record_tab"):
                  text.index("def _build_keys_tab")]
    assert "v_hold" not in record
    assert "v_hotkey" not in record
    system = text[text.index("def _build_system_tab"):
                  text.index("def _fit_to_content")]
    assert "v_hotkey" not in system
    assert "v_hold" not in system


def test_every_key_row_is_described():
    keys = _method(_source(), "_build_keys_tab")
    for key in KEYS:
        assert key in keys, key
    for combo in COMBOS:
        assert '"' + combo + '"' in keys, combo


def test_keys_are_in_both_locales():
    for lang in ("en", "ru"):
        path = LOCALES / (lang + ".json")
        with open(path, encoding="utf-8") as handle:
            catalog = json.load(handle)
        for key in KEYS:
            assert str(catalog.get(key, "")).strip(), (lang, key)


def test_class_titles_are_in_both_locales():
    keys = ("tabs.keys", "settings.sub_keys", "settings.g_keys_dictation",
            "settings.g_keys_panel", "settings.g_keys_paste",
            "settings.g_keys_settings", "settings.g_keys_history")
    for lang in ("en", "ru"):
        path = LOCALES / (lang + ".json")
        with open(path, encoding="utf-8") as handle:
            catalog = json.load(handle)
        for key in keys:
            assert str(catalog.get(key, "")).strip(), (lang, key)
