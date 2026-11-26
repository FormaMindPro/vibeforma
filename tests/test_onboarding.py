"""T7: hotkey validation, safe defaults, and the microphone test path.

No tkinter, no pynput, no model: the pure helpers and the service's test
branch are checked with substituted transcribers and modules.
"""
import logging
import sys
import threading
import types

import main
import onboarding
import pytest


@pytest.fixture(autouse=True)
def fake_keyboard(monkeypatch):
    """A deterministic pynput backend: other test files substitute their own."""
    keyboard = types.ModuleType("pynput.keyboard")

    class HotKey:
        @staticmethod
        def parse(combo):
            if not combo or "++" in combo:
                raise ValueError(combo)
            return combo.strip("<>").split("+><")

    keyboard.HotKey = HotKey
    pynput = types.ModuleType("pynput")
    pynput.keyboard = keyboard
    monkeypatch.setitem(sys.modules, "pynput", pynput)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", keyboard)


class FakeASR:
    def __init__(self, text):
        self.text = text

    def transcribe(self, buf, verbose=True):
        return self.text


class FakeRec:
    def had_speech(self):
        return True


def make_service(text="hello"):
    svc = main.Service.__new__(main.Service)
    svc.cfg = {"notify_text": False}
    svc.log = logging.getLogger("test-onboarding")
    svc.live = {"state": main.STATE_PROCESSING, "partial": "",
                "level": 0.0, "test_text": "", "test_state": ""}
    svc.state = main.STATE_PROCESSING
    svc._state_lock = threading.Lock()
    svc._cancel_evt = threading.Event()
    svc.cancel_watcher = None
    svc.tray = None
    svc.pending_update = None
    svc._update_busy = False
    svc._running = True
    svc._test_mode = True
    svc.asr = FakeASR(text)
    return svc


def test_valid_combo_is_accepted():
    ok, code, warning = onboarding.validate_hotkey("ctrl+alt+space")
    assert ok is True
    assert code == "ok"
    assert warning == ""


def test_modifiers_only_is_rejected():
    ok, code, _warning = onboarding.validate_hotkey("ctrl+alt+shift")
    assert ok is False
    assert code == "modifiers_only"


def test_empty_hotkey_is_rejected():
    assert onboarding.validate_hotkey("") == (False, "empty", "")
    assert onboarding.validate_hotkey("  +  ") == (False, "empty", "")


def test_reserved_combo_is_warned():
    ok, code, warning = onboarding.validate_hotkey("Alt+F4")
    assert ok is True
    assert code == "ok"
    assert warning == "conflict"


def test_two_keys_are_rejected():
    ok, code, _warning = onboarding.validate_hotkey("ctrl+a+b")
    assert ok is False
    assert code == "invalid"


def test_normalize_hotkey():
    assert onboarding.normalize_hotkey("Alt+Ctrl+Space") == "ctrl+alt+space"
    assert onboarding.normalize_hotkey("ctrl+ctrl+alt+x") == "ctrl+alt+x"
    assert onboarding.normalize_hotkey("") == ""


def test_onboarding_needed_by_default():
    assert onboarding.needed({}) is True
    assert onboarding.needed({"onboarding_done": False}) is True
    assert onboarding.needed({"onboarding_done": True}) is False


def test_apply_choices_falls_back_to_safe_values():
    out = onboarding.apply_choices(
        {"hotkey": "ctrl+alt+space", "language": "ru"},
        language="xx", hotkey="ctrl+alt", locale="ru")
    assert out["language"] == "en"
    assert out["hotkey"] == "ctrl+alt+space"
    assert out["locale"] == "ru"
    assert out["onboarding_done"] is True


def test_skip_keeps_current_values():
    cfg = {"hotkey": "ctrl+shift+f9", "language": "de",
           "input_device": "USB Mic", "locale": "auto"}
    out = onboarding.apply_choices(cfg, {})
    assert out["hotkey"] == "ctrl+shift+f9"
    assert out["language"] == "de"
    assert out["input_device"] == "USB Mic"
    assert out["onboarding_done"] is True


def test_mic_test_does_not_insert(monkeypatch):
    bomb = types.ModuleType("insert")

    def _forbidden(*args, **kwargs):
        raise AssertionError("the microphone test must not insert text")

    bomb.insert_text = _forbidden
    monkeypatch.setitem(sys.modules, "insert", bomb)
    svc = make_service("hello world")

    main.Service._transcribe_worker(svc, b"audio", FakeRec())

    assert svc.live["test_text"] == "hello world"
    assert svc.live["test_state"] == "done"
    assert svc._test_mode is False
    assert svc.state == main.STATE_IDLE


def test_mic_test_empty_is_reported(monkeypatch):
    bomb = types.ModuleType("insert")
    bomb.insert_text = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no insertion"))
    monkeypatch.setitem(sys.modules, "insert", bomb)
    svc = make_service("")

    main.Service._transcribe_worker(svc, b"audio", FakeRec())

    assert svc.live["test_text"] == ""
    assert svc.live["test_state"] == "empty"
    assert svc._test_mode is False


# -- combo aliases ---------------------------------------------------------
# The wizard must take the names a person types ("control", "win", "escape")
# and canonicalize them the same way app/hotkey.py does, from the same table.


def test_validate_hotkey_accepts_hotkey_aliases():
    assert onboarding.validate_hotkey("control+alt+space") == (True, "ok", "")
    assert onboarding.validate_hotkey("win+escape") == (True, "ok", "")


def test_hotkey_aliases_normalize_to_canonical_combos():
    assert onboarding.normalize_hotkey("control+alt+space") == "ctrl+alt+space"
    assert onboarding.normalize_hotkey("win+escape") == "cmd+esc"
    assert onboarding.normalize_hotkey("shift+return") == "shift+enter"


def test_reserved_warning_keeps_hotkey_aliases():
    ok, code, warning = onboarding.validate_hotkey("win+l")
    assert ok is True
    assert code == "ok"
    assert warning == "conflict"


def test_onboarding_imports_no_pynput_for_hotkey_aliases():
    import ast
    import pathlib

    source = pathlib.Path(onboarding.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "pynput" not in imported
