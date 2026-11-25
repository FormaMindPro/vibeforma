"""The by-app profiles section of the Dictionary tab (card-252).

A profile is built in the settings window: a target (process and/or window
fragment), its own terms and snippets. Save writes the list to
``config.json`` under ``app_profiles``; Reset clears it. The live tests build
only the Dictionary tab on a bare Tk root, with the frequent-correction store
stubbed out.
"""
import json
import sys
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("settings_window uses Windows APIs", allow_module_level=True)

import config
import i18n
import settings_window

tk = pytest.importorskip("tkinter")

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

PROFILE_WORD = "\u043f\u0440\u043e\u0444\u0438\u043b\u044c"   # Russian "profile"

KEYS = (
    "profiles.g",
    "profiles.note",
    "profiles.name",
    "profiles.process",
    "profiles.window",
    "profiles.add",
    "profiles.apply",
    "profiles.remove",
    "profiles.empty",
    "profiles.select",
    "profiles.select_entry",
    "profiles.no_target",
    "profiles.dup_name",
    "profiles.limit",
    "profiles.added",
    "profiles.updated",
    "profiles.removed",
    "profiles.terms",
    "profiles.term_added",
    "profiles.term_removed",
    "profiles.snippets",
    "profiles.snippet_added",
    "profiles.snippet_removed",
)


def _source():
    return SOURCE.read_text(encoding="utf-8")


def _method(text, name):
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


def _catalog(lang):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        return json.load(handle)


# -- the section is wired into the window ----------------------------------
def test_the_profiles_section_lives_on_the_dictionary_tab():
    text = _source()
    dictionary = _method(text, "_build_dictionary_tab")
    assert "self._build_profiles_section(f)" in dictionary
    section = _method(text, "_build_profiles_section")
    for name in ("profiles.g", "profiles.note", "self.profile_list",
                 "profiles.terms", "profiles.snippets",
                 "self._add_profile", "self._remove_profile",
                 "self._add_profile_term", "self._add_profile_snippet"):
        assert name in section, name


def test_save_writes_the_profile_list():
    save = _method(_source(), "_save")
    assert 'c["app_profiles"] = normalize_app_profiles(self.app_profiles)' in save


def test_reset_clears_the_profile_list():
    text = _source()
    reset = _method(text, "_reset")
    assert "self._reset_profiles()" in reset
    helper = _method(text, "_reset_profiles")
    assert "self.app_profiles = []" in helper
    assert "self._refresh_profiles()" in helper


def test_the_heading_carries_the_search_word():
    section = _method(_source(), "_build_profiles_section")
    assert 'self._head(f, self.tr("profiles.g"))' in section
    assert PROFILE_WORD in _catalog("ru")["profiles.g"].casefold()
    assert "profile" in _catalog("en")["profiles.g"].casefold()


def test_every_profile_key_is_localized():
    for lang in ("en", "ru"):
        catalog = _catalog(lang)
        missing = [key for key in KEYS if not str(catalog.get(key, "")).strip()]
        assert not missing, (lang, missing)


# -- the tab on a live Tk root ---------------------------------------------
class _ProfileHost(settings_window.SettingsWindow):
    """Only the Dictionary tab, built on a bare class shell."""

    def __init__(self, root, cfg):
        self.win = root
        self.cfg = cfg
        self.tr = lambda key, **kw: i18n.t(cfg, key, **kw)
        self._px = lambda value: int(round(value))
        self.rows = {}
        self._search_index = []
        self._frame_key = {}
        frame = tk.Frame(root)
        self._frame_key[frame] = "dictionary"
        frame.pack()
        self._build_dictionary_tab(frame)


@pytest.fixture(scope="module")
def tk_root():
    # One Tk root for the module: a fresh Tk() after a destroyed root can hit
    # the "invalid command name tcl_findLibrary" interpreter re-init bug.
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for a live Tk widget test")
    root.withdraw()
    yield root
    root.destroy()


def _clear_root(root):
    for child in root.winfo_children():
        child.destroy()


@pytest.fixture()
def tab(tk_root, monkeypatch):
    monkeypatch.setattr(settings_window.dictlearn, "suggestions", lambda: [])
    host = _ProfileHost(tk_root, {"locale": "en", "dictionary": [],
                                  "snippets": [], "app_profiles": []})
    yield host
    _clear_root(tk_root)


@pytest.fixture()
def saved_tab(tk_root, monkeypatch):
    monkeypatch.setattr(settings_window.dictlearn, "suggestions", lambda: [])
    host = _ProfileHost(tk_root, {
        "locale": "en", "dictionary": [], "snippets": [],
        "app_profiles": [{"name": "Docs", "window": "report",
                          "dictionary": [{"from": "a", "to": "b"}]}],
    })
    yield host
    _clear_root(tk_root)


def _add_editor(host):
    host.v_profile_name.set("Editor")
    host.v_profile_process.set("code.exe")
    host._add_profile()
    assert len(host.app_profiles) == 1
    assert host.profile_list.curselection() == (0,)


def test_the_heading_is_in_the_search_index(tab):
    entries = [(text, key) for text, key, _label, _fg in tab._search_index]
    assert (tab.tr("profiles.g"), "dictionary") in entries


def test_a_profile_and_its_term_are_listed(tab):
    _add_editor(tab)
    assert tab.app_profiles[0]["process"] == "code.exe"
    assert "Editor" in tab.profile_list.get(0)
    assert "code.exe" in tab.profile_list.get(0)

    tab.v_profile_from.set("vibra")
    tab.v_profile_to.set("VibeForma")
    tab._add_profile_term()

    assert tab.app_profiles[0]["dictionary"] == [
        {"from": "vibra", "to": "VibeForma"}]
    assert "vibra" in tab.profile_term_list.get(0)
    assert tab.v_profile_from.get() == ""


def test_a_profile_without_a_target_is_rejected(tab):
    tab.v_profile_name.set("No target")
    tab._add_profile()
    assert tab.app_profiles == []
    assert tab.profile_status.cget("text") == tab.tr("profiles.no_target")


def test_a_duplicate_name_is_rejected(tab):
    _add_editor(tab)
    tab.v_profile_name.set("editor")
    tab.v_profile_process.set("other.exe")
    tab._add_profile()
    assert len(tab.app_profiles) == 1
    assert tab.profile_status.cget("text") == tab.tr(
        "profiles.dup_name", name="editor")


def test_applying_fields_changes_the_target_and_keeps_the_terms(tab):
    _add_editor(tab)
    tab.v_profile_from.set("vibra")
    tab.v_profile_to.set("VibeForma")
    tab._add_profile_term()

    tab.v_profile_process.set("notepad.exe")
    tab._apply_profile()

    assert tab.app_profiles[0]["process"] == "notepad.exe"
    assert tab.app_profiles[0]["dictionary"] == [
        {"from": "vibra", "to": "VibeForma"}]


def test_a_profile_keeps_its_own_snippets(tab):
    _add_editor(tab)
    tab.v_profile_trigger.set("sig")
    tab.v_profile_text.set("Best regards")
    tab._add_profile_snippet()
    assert tab.app_profiles[0]["snippets"] == [
        {"trigger": "sig", "text": "Best regards"}]
    assert "sig" in tab.profile_snippet_list.get(0)


def test_a_term_can_be_removed_from_the_selected_profile(tab):
    _add_editor(tab)
    for term in ("vibra", "teh"):
        tab.v_profile_from.set(term)
        tab.v_profile_to.set(term.upper())
        tab._add_profile_term()
    tab.profile_term_list.selection_set(0)
    tab._remove_profile_term()
    assert tab.app_profiles[0]["dictionary"] == [
        {"from": "teh", "to": "TEH"}]


def test_removing_the_profile_returns_an_empty_list(tab):
    _add_editor(tab)
    tab._remove_profile()
    assert tab.app_profiles == []
    assert tab.profile_list.size() == 0
    assert tab.profile_status.cget("text") == tab.tr(
        "profiles.removed", name="Editor")


def test_reset_returns_an_empty_profile_list(tab):
    _add_editor(tab)
    tab._reset_profiles()
    assert tab.app_profiles == []
    assert tab.profile_list.size() == 0
    assert tab.v_profile_name.get() == ""


def test_profiles_are_read_from_the_config_on_build(saved_tab):
    assert saved_tab.app_profiles[0]["name"] == "Docs"
    assert saved_tab.profile_list.size() == 1
    saved_tab.profile_list.selection_set(0)
    saved_tab._load_profile()
    assert saved_tab.v_profile_window.get() == "report"
    assert saved_tab.profile_term_list.size() == 1


def test_the_normalizer_is_the_one_from_the_core():
    assert settings_window.normalize_app_profiles is config.normalize_app_profiles
