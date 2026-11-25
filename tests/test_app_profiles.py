"""By-app profiles (card-251): targets, merge and selection by target window.

A profile binds a dictionary and snippets to a process name and/or a window
fragment. The profile is chosen by the address captured when the recording
starts (``_target_hwnd``), never by the window that is foreground when the
text is ready; a window without a profile keeps the global rules.
"""
import sys
import types

import config
import dictionary
from vibeforma.service import Service

TERM = "\u0442\u0435\u0440\u043c\u0438\u043d"   # a word the global rule renames


class _Log:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class _Recorder:
    def had_speech(self):
        return True


class _Asr:
    """A fake engine that returns one text for every take."""

    def __init__(self, text):
        self.text = text

    def transcribe(self, _samples):
        return self.text


def _fake_insert(monkeypatch, identities, foreground=999):
    """A stand-in for insert; the window identity comes from the test."""
    module = types.ModuleType("insert")
    module.RESULT_PASTED = "pasted"
    module.RESULT_COPIED = "copied"
    module.RESULT_CANCELLED = "cancelled"
    module.get_foreground_hwnd = lambda: foreground
    module.window_identity = lambda hwnd: identities.get(hwnd, ("", ""))
    module.insert_text = lambda text, **_kwargs: "pasted"
    monkeypatch.setitem(sys.modules, "insert", module)
    return module


def _service(**overrides):
    cfg = {
        "locale": "en",
        "dictionary": [],
        "snippets": [],
        "app_profiles": [],
        "history": False,
        "hud_privacy": False,
        "notify_text": False,
        "confirm_focus_change": False,
    }
    cfg.update(overrides)
    svc = Service(cfg, _Log())
    svc.stats = None
    svc.state = "processing"
    return svc


# -- the config format -----------------------------------------------------
def test_config_default_is_an_empty_profile_list():
    assert config.DEFAULTS["app_profiles"] == []


def test_normalize_keeps_targets_and_cleans_the_inner_lists():
    profiles = config.normalize_app_profiles([{
        "name": "Editor",
        "process": "C:\\Apps\\Code.exe",
        "dictionary": [{"from": "vibra", "to": "VibeForma"},
                       {"from": "", "to": "x"}],
        "snippets": [{"trigger": "sig", "text": "Best"}],
        "unknown": 1,
    }])
    assert profiles == [{
        "name": "Editor",
        "process": "C:\\Apps\\Code.exe",
        "window": "",
        "dictionary": [{"from": "vibra", "to": "VibeForma"}],
        "snippets": [{"trigger": "sig", "text": "Best"}],
    }]


def test_a_profile_without_a_target_is_dropped():
    assert config.normalize_app_profiles([
        "not a profile",
        {"name": "No target", "dictionary": [{"from": "a", "to": "b"}]},
        {},
    ]) == []


def test_a_profile_without_a_name_falls_back_to_its_target():
    profiles = config.normalize_app_profiles([{"process": "notepad.exe"}])
    assert profiles[0]["name"] == "notepad.exe"


def test_a_window_only_profile_is_valid():
    profiles = config.normalize_app_profiles([
        {"name": "Docs", "window": "my report"}])
    assert profiles[0]["process"] == ""
    assert config.match_app_profile(profiles, "browser.exe",
                                    "My Report - Browser")["name"] == "Docs"


def test_normalize_drops_duplicate_names_and_caps_the_list():
    raw = [{"name": "Same", "process": "a.exe"},
           {"name": "same", "process": "b.exe"}]
    raw += [{"name": f"p{i}", "process": f"app{i}.exe"} for i in range(60)]
    out = config.normalize_app_profiles(raw)
    assert [profile["name"] for profile in out[:1]] == ["Same"]
    assert len(out) == config.MAX_APP_PROFILES


def test_validate_reports_a_profile_without_a_target():
    issues = config.validate_app_profiles([{"name": "No target"}, "junk"])
    assert [(issue["level"], issue["code"]) for issue in issues] == [
        ("error", "format"), ("error", "format")]


def test_validate_reports_a_repeated_name():
    issues = config.validate_app_profiles([
        {"name": "Editor", "process": "a.exe"},
        {"name": "editor", "window": "B"}])
    assert [(issue["level"], issue["code"]) for issue in issues] == [
        ("error", "duplicate")]


def test_validate_accepts_a_complete_profile():
    assert config.validate_app_profiles([
        {"name": "Editor", "process": "code.exe",
         "dictionary": [{"from": "a", "to": "b"}]}]) == []


# -- matching a window -----------------------------------------------------
def test_match_by_process_ignores_case_and_the_exe_suffix():
    profiles = [{"name": "Editor", "process": "Code"}]
    assert config.match_app_profile(
        profiles, "code.exe", "anything")["name"] == "Editor"


def test_match_by_a_window_title_fragment():
    profiles = [{"name": "Docs", "window": "my report"}]
    assert config.match_app_profile(
        profiles, "browser.exe", "My Report - Browser")["name"] == "Docs"
    assert config.match_app_profile(profiles, "browser.exe", "Inbox") is None


def test_both_targets_must_match_when_both_are_set():
    profiles = [{"name": "Editor", "process": "code.exe",
                 "window": "project"}]
    assert config.match_app_profile(
        profiles, "code.exe", "project - Visual Studio Code")
    assert config.match_app_profile(profiles, "code.exe", "settings") is None
    assert config.match_app_profile(profiles, "notepad.exe", "project") is None


def test_the_first_matching_profile_wins():
    profiles = [{"name": "First", "process": "a.exe"},
                {"name": "Second", "window": "Window"}]
    assert config.match_app_profile(
        profiles, "a.exe", "Window")["name"] == "First"


def test_an_empty_window_matches_no_profile():
    assert config.match_app_profile(
        [{"name": "Editor", "process": "a.exe"}]) is None


def test_no_handle_has_no_identity():
    import insert

    assert insert.window_identity(None) == ("", "")
    assert insert.window_identity(0) == ("", "")


# -- the take picks the profile of its target window -----------------------
def test_the_profile_is_chosen_by_the_target_window(monkeypatch):
    identities = {
        111: ("editor.exe", "notes.txt - Editor"),
        222: ("browser.exe", "Inbox - Browser"),
        999: ("other.exe", "Other"),
    }
    profiles = [
        {"name": "Editor", "process": "editor.exe",
         "dictionary": [{"from": TERM, "to": "Profile"}]},
        {"name": "Browser", "process": "browser.exe",
         "dictionary": [{"from": TERM, "to": "Browser"}]},
        {"name": "Other", "process": "other.exe",
         "dictionary": [{"from": TERM, "to": "Other"}]},
    ]
    _fake_insert(monkeypatch, identities, foreground=999)
    svc = _service(dictionary=[{"from": TERM, "to": "Global"}],
                   app_profiles=profiles)
    svc.asr = _Asr(TERM)

    # The foreground is 999 with its own profile the whole time: a profile
    # read from it (or a constant global fallback) fails the first assert.
    svc._target_hwnd = 111
    svc._panel_mode = True
    svc._transcribe_worker(object(), _Recorder())
    assert svc.live["draft"] == "Profile"

    svc._target_hwnd = 222
    svc._panel_mode = True
    svc._transcribe_worker(object(), _Recorder())
    assert svc.live["draft"] == "Browser"

    # A window without a profile keeps the global rule.
    svc._target_hwnd = 333
    svc._panel_mode = True
    svc._transcribe_worker(object(), _Recorder())
    assert svc.live["draft"] == "Global"


def test_profile_snippets_expand_only_in_their_window(monkeypatch):
    identities = {111: ("editor.exe", "Editor"),
                  222: ("browser.exe", "Browser")}
    profiles = [{"name": "Editor", "process": "editor.exe",
                 "snippets": [{"trigger": "sig", "text": "PROFILE"}]}]
    _fake_insert(monkeypatch, identities)
    svc = _service(app_profiles=profiles)
    svc.asr = _Asr("sig")

    svc._target_hwnd = 111
    svc._panel_mode = True
    svc._transcribe_worker(object(), _Recorder())
    assert svc.live["draft"] == "PROFILE"

    svc._target_hwnd = 222
    svc._panel_mode = True
    svc._transcribe_worker(object(), _Recorder())
    assert svc.live["draft"] == "sig"


def test_an_unreadable_window_keeps_the_global_rules(monkeypatch):
    _fake_insert(monkeypatch, {})
    svc = _service(dictionary=[{"from": TERM, "to": "Global"}],
                   app_profiles=[{"name": "Editor", "process": "editor.exe",
                                  "dictionary": [{"from": TERM,
                                                  "to": "Profile"}]}])
    svc.asr = _Asr(TERM)
    svc._target_hwnd = 123
    svc._panel_mode = True

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["draft"] == "Global"


# -- one hotwords set without duplicates -----------------------------------
def test_the_profile_terms_join_the_context_without_duplicates(monkeypatch):
    _fake_insert(monkeypatch, {111: ("editor.exe", "Editor")})
    svc = _service(
        hotwords="Forma",
        app_profiles=[{"name": "Editor", "process": "editor.exe",
                       "dictionary": [{"from": "vibra",
                                       "to": "VibeForma"}]}])
    svc._target_hwnd = 111
    svc._context_hotwords = "Forma, VibeForma"
    engine = types.SimpleNamespace(cfg={"hotwords": "Forma",
                                        "dictionary": []})
    original = engine.cfg

    with svc._context_engine(engine):
        assert engine.cfg is not original
        assert engine.cfg["hotwords"] == "Forma, VibeForma"
        assert engine.cfg["dictionary"] == [
            {"from": "vibra", "to": "VibeForma"}]
        merged = dictionary.merge_hotwords(engine.cfg["hotwords"],
                                           engine.cfg["dictionary"])
        assert merged == "Forma, VibeForma"

    assert engine.cfg is original
