"""Hold-to-talk: combo state, listener choice and service transitions."""
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import hotkey as hotkey_mod
import pytest
from config import DEFAULTS
from hotkey import ComboTracker, HoldHotkey, key_token, parse_combo
from vibeforma import (
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
)
from vibeforma.app import build_hotkey, hotkey_mode
from vibeforma.service import Service

ROOT = Path(__file__).resolve().parents[1]


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


class FakeKey:
    """A pynput-like key: a name for special keys, a char for typed ones."""

    def __init__(self, name=None, char=None):
        self.name = name
        self.char = char


class FakeTray:
    def __init__(self):
        self.messages = []
        self.states = []

    def notify(self, message, title="VibeForma"):
        self.messages.append(message)

    def set_state(self, state):
        self.states.append(state)


class FakeWatcher:
    """Mirrors EscWatcher's idempotent start/stop contract."""

    def __init__(self):
        self.active = False
        self.events = []

    def start(self):
        if not self.active:
            self.active = True
            self.events.append("start")

    def stop(self):
        if self.active:
            self.active = False
            self.events.append("stop")


class FakeRecorder:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self, device_index):
        self.started = True

    def had_speech(self):
        return True

    def stop(self):
        self.stopped = True
        return object()


class FakeASR:
    def transcribe(self, _buf, verbose=True):
        return "hello"


class FakeHistory:
    def __init__(self):
        self.added = []

    def add(self, text, app="", way="insert"):
        self.added.append(text)

    def load(self):
        return []


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def make_service(monkeypatch):
    cfg = {
        "locale": "en",
        "dictionary": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
        "history_app": False,
        "input_device": "",
        "beep_on": False,
        "beep_off": False,
    }
    svc = Service(cfg, RecordingLog())
    svc.history = FakeHistory()
    svc.tray = FakeTray()
    watcher = FakeWatcher()
    svc.attach_cancel_watcher(watcher)
    svc.asr = FakeASR()

    recorders = []

    def make_recorder():
        recorder = FakeRecorder()
        recorders.append(recorder)
        return recorder

    svc._make_recorder = make_recorder
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.query_devices = lambda index: {"name": "Fake microphone"}
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    fake_insert = types.ModuleType("insert")
    fake_insert.RESULT_PASTED = "pasted"
    fake_insert.RESULT_COPIED = "copied"
    fake_insert.RESULT_CANCELLED = "cancelled"
    fake_insert.get_foreground_hwnd = lambda: 7
    fake_insert.is_own_window = lambda hwnd: False
    fake_insert.foreground_window = lambda: ""
    fake_insert.insert_text = lambda text, **kwargs: "pasted"
    monkeypatch.setitem(sys.modules, "insert", fake_insert)
    return svc, recorders, watcher


# -- combo tracker --------------------------------------------------------
def test_parse_combo_normalizes_tokens():
    assert parse_combo("Ctrl+Alt+Space") == ("ctrl", "alt", "space")
    assert parse_combo(" ctrl + <alt> + space ") == ("ctrl", "alt", "space")
    assert parse_combo("ctrl+ctrl+a") == ("ctrl", "a")
    assert parse_combo("") == ()


def test_combo_down_fires_once_and_auto_repeat_is_ignored():
    tracker = ComboTracker("ctrl+alt+space")

    assert tracker.press("ctrl") is False
    assert tracker.press("alt") is False
    assert tracker.press("space") is True
    assert tracker.down is True

    assert tracker.press("space") is False        # auto-repeat of the last key
    assert tracker.press("alt") is False          # repeated modifier
    assert tracker.down is True


def test_any_combo_key_release_ends_the_combo():
    tracker = ComboTracker("ctrl+alt+space")
    for token in ("ctrl", "alt", "space"):
        tracker.press(token)

    assert tracker.release("alt") is True
    assert tracker.down is False
    assert tracker.release("alt") is False
    assert tracker.release("ctrl") is False
    assert tracker.release("space") is False


def test_foreign_keys_do_not_break_the_state():
    tracker = ComboTracker("ctrl+space")
    tracker.press("ctrl")
    assert tracker.press("x") is False
    assert tracker.release("x") is False
    assert tracker.down is False

    assert tracker.press("space") is True
    assert tracker.press("y") is False
    assert tracker.release("y") is False
    assert tracker.down is True
    assert tracker.release("space") is True


def test_tracker_resets_after_up():
    tracker = ComboTracker("ctrl+space")

    assert tracker.press("ctrl") is False
    assert tracker.press("space") is True
    assert tracker.release("ctrl") is True
    assert tracker.down is False

    # The other key is still held: pressing the released one reopens the combo.
    assert tracker.press("ctrl") is True
    assert tracker.press("space") is False
    assert tracker.release("space") is True

    assert tracker.press("ctrl") is False
    assert tracker.press("space") is True


def test_partial_press_and_release_do_not_emit_up():
    tracker = ComboTracker("ctrl+space")
    assert tracker.press("ctrl") is False
    assert tracker.release("ctrl") is False
    assert tracker.down is False


def test_reset_forgets_held_keys_without_an_event():
    tracker = ComboTracker("ctrl+space")
    tracker.press("ctrl")
    tracker.press("space")
    assert tracker.down is True

    tracker.reset()

    assert tracker.down is False
    assert tracker.press("space") is False
    assert tracker.press("ctrl") is True


def test_key_token_maps_pynput_style_keys():
    assert key_token(FakeKey(name="ctrl_l")) == "ctrl"
    assert key_token(FakeKey(name="alt_gr")) == "alt"
    assert key_token(FakeKey(name="space")) == "space"
    assert key_token(FakeKey(name="f9")) == "f9"
    assert key_token(FakeKey(char="A")) == "a"
    assert key_token(FakeKey(char=" ")) == ""
    assert key_token("Ctrl") == "ctrl"


# -- listener choice ------------------------------------------------------
def test_hotkey_mode_reads_the_config():
    assert hotkey_mode({"hold_to_talk": True}) == "hold"
    assert hotkey_mode({"hold_to_talk": False}) == "toggle"
    assert hotkey_mode({}) == "toggle"


def test_hold_is_off_by_default():
    assert DEFAULTS["hold_to_talk"] is False


def test_factory_returns_hold_or_toggle_listener():
    def noop():
        pass

    log = RecordingLog()
    toggle = hotkey_mod.make_hotkey("ctrl+alt+space", False,
                                    noop, noop, noop, log)
    hold = hotkey_mod.make_hotkey("ctrl+alt+space", True,
                                  noop, noop, noop, log)

    assert isinstance(toggle, hotkey_mod.Hotkey)
    assert isinstance(hold, hotkey_mod.HoldHotkey)


def test_build_hotkey_passes_the_mode_to_the_factory(monkeypatch):
    calls = []

    def fake_make(combo, hold, on_toggle, on_press, on_release, log):
        calls.append((combo, hold, on_toggle, on_press, on_release, log))
        return "listener"

    fake = types.ModuleType("hotkey")
    fake.make_hotkey = fake_make
    monkeypatch.setitem(sys.modules, "hotkey", fake)
    svc = SimpleNamespace(on_hotkey=lambda: "toggle",
                          on_hotkey_press=lambda: "press",
                          on_hotkey_release=lambda: "release")
    log = object()

    assert build_hotkey({"hotkey": "ctrl+alt+space", "hold_to_talk": True},
                        svc, log) == "listener"
    assert calls[-1][:2] == ("ctrl+alt+space", True)
    assert calls[-1][2:5] == (svc.on_hotkey, svc.on_hotkey_press,
                              svc.on_hotkey_release)
    assert calls[-1][5] is log

    assert build_hotkey({"hotkey": "ctrl+alt+space"}, svc, log) == "listener"
    assert calls[-1][1] is False


def test_hold_listener_fires_down_and_up_once(monkeypatch):
    listeners = []

    class FakeListener:
        def __init__(self, on_press=None, on_release=None):
            self.on_press = on_press
            self.on_release = on_release
            self.daemon = False
            self.started = False
            self.stopped = False
            listeners.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(hotkey_mod.keyboard, "Listener", FakeListener)
    downs, ups = [], []
    hold = HoldHotkey("ctrl+alt+space", lambda: downs.append(1),
                      lambda: ups.append(1), RecordingLog())
    hold.start()
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener.started is True
    assert listener.daemon is True

    ctrl = FakeKey(name="ctrl_l")
    alt = FakeKey(name="alt_l")
    space = FakeKey(name="space")
    listener.on_press(ctrl)
    listener.on_press(alt)
    listener.on_press(space)
    listener.on_press(space)                 # auto-repeat
    listener.on_press(FakeKey(char="x"))     # foreign key
    assert downs == [1]

    listener.on_release(FakeKey(char="x"))
    assert ups == []
    listener.on_release(space)
    listener.on_release(space)
    assert ups == [1]

    hold.stop()
    assert listener.stopped is True


# -- service transitions --------------------------------------------------
def test_press_and_release_round_trip(monkeypatch):
    svc, recorders, watcher = make_service(monkeypatch)
    svc._set_state(STATE_IDLE)

    svc.on_hotkey_press()
    assert svc.state == STATE_RECORDING
    assert len(recorders) == 1
    assert recorders[0].started is True
    assert watcher.active is True

    svc.on_hotkey_release()
    assert recorders[0].stopped is True
    assert wait_until(lambda: svc.state == STATE_IDLE)
    assert wait_until(lambda: watcher.active is False)
    assert svc.tray.messages == ["Inserted, 5 chars"]


def test_second_press_while_recording_does_not_start_another(monkeypatch):
    svc, recorders, _watcher = make_service(monkeypatch)
    svc._set_state(STATE_IDLE)

    svc.on_hotkey_press()
    svc.on_hotkey_press()

    assert svc.state == STATE_RECORDING
    assert len(recorders) == 1


def test_auto_repeat_never_reaches_the_service(monkeypatch):
    svc, recorders, _watcher = make_service(monkeypatch)
    svc._set_state(STATE_IDLE)
    tracker = ComboTracker("ctrl+alt+space")

    for token in ("ctrl", "alt", "space", "space", "space"):
        if tracker.press(token):
            svc.on_hotkey_press()

    assert len(recorders) == 1
    assert svc.state == STATE_RECORDING


@pytest.mark.parametrize("state", [STATE_PAUSED, STATE_LOADING,
                                   STATE_PROCESSING, STATE_RECORDING])
def test_press_outside_idle_is_ignored(monkeypatch, state):
    svc, recorders, _watcher = make_service(monkeypatch)
    svc._set_state(state)

    svc.on_hotkey_press()

    assert svc.state == state
    assert recorders == []


@pytest.mark.parametrize("state", [STATE_IDLE, STATE_PAUSED, STATE_LOADING,
                                   STATE_PROCESSING])
def test_release_outside_recording_is_ignored(monkeypatch, state):
    svc, _recorders, _watcher = make_service(monkeypatch)
    svc._set_state(state)
    calls = []
    svc.stop_recording = lambda reason: calls.append(reason)

    svc.on_hotkey_release()

    assert calls == []


def test_release_from_recording_stops_with_hold(monkeypatch):
    svc, _recorders, _watcher = make_service(monkeypatch)
    svc._set_state(STATE_RECORDING)
    calls = []
    svc.stop_recording = lambda reason: calls.append(reason)

    svc.on_hotkey_release()

    assert calls == ["hold"]


# -- settings surface -----------------------------------------------------
def test_record_tab_carries_the_hold_switch():
    source = (ROOT / "app" / "settings_window.py").read_text(encoding="utf-8")
    block = source[source.index("def _build_record_tab"):
                   source.index("def _build_model_tab")]
    assert "v_hold" in block
    assert "settings.hold_to_talk" in block
    assert "settings.note_hold_to_talk" in block
    save = source[source.index("def _save"):]
    assert 'c["hold_to_talk"]' in save


@pytest.mark.parametrize("lang", ("en", "ru"))
def test_hold_keys_are_in_both_locales(lang):
    path = ROOT / "app" / "locales" / f"{lang}.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for key in ("settings.hold_to_talk", "settings.note_hold_to_talk"):
        assert catalog[key].strip()
