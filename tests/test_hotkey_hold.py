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
def test_keys_tab_carries_the_hold_switch():
    source = (ROOT / "app" / "settings_window.py").read_text(encoding="utf-8")
    block = source[source.index("def _build_keys_tab"):
                   source.index("def _build_model_tab")]
    assert "v_hold" in block
    assert "settings.hold_to_talk" in block
    assert "settings.note_hold_to_talk" in block
    record = source[source.index("def _build_record_tab"):
                    source.index("def _build_keys_tab")]
    assert "v_hold" not in record
    save = source[source.index("def _save"):]
    assert 'c["hold_to_talk"]' in save


@pytest.mark.parametrize("lang", ("en", "ru"))
def test_hold_keys_are_in_both_locales(lang):
    path = ROOT / "app" / "locales" / f"{lang}.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for key in ("settings.hold_to_talk", "settings.note_hold_to_talk"):
        assert catalog[key].strip()


# -- worker thread ---------------------------------------------------------
# Like the real pynput, the hook callbacks below run on the listener's own
# thread; the tests drive them through submit() and wait on events.


def install_hook_thread_listener(monkeypatch):
    """Fake pynput Listener: a real thread, jobs run as hook callbacks."""
    import queue as queue_mod
    import threading

    listeners = []

    class HookThreadListener(threading.Thread):
        def __init__(self, on_press=None, on_release=None):
            super().__init__(daemon=True)
            self.on_press = on_press
            self.on_release = on_release
            self.hook_started = False
            self.hook_stopped = False
            self._jobs = queue_mod.Queue()

        def run(self):
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                job()

        def submit(self, fn):
            self._jobs.put(fn)

        def start(self):
            self.hook_started = True
            super().start()

        def stop(self):
            self.hook_stopped = True
            self._jobs.put(None)

    def factory(*args, **kwargs):
        listener = HookThreadListener(*args, **kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(hotkey_mod.keyboard, "Listener", factory)
    return listeners


def threads_named(name):
    import threading

    return [t for t in threading.enumerate() if t.name == name]


def test_hold_worker_gets_the_event_and_the_hook_thread_returns_at_once(
        monkeypatch):
    import threading

    listeners = install_hook_thread_listener(monkeypatch)
    gate = threading.Event()
    started = threading.Event()
    hook_returned = threading.Event()
    handler_threads = []

    def slow_press():
        handler_threads.append(threading.current_thread().name)
        started.set()
        gate.wait(2.0)

    hold = HoldHotkey("ctrl+space", slow_press, lambda: None, RecordingLog())
    hold.start()
    listener = listeners[0]

    def hook_job():
        listener.on_press(FakeKey(name="ctrl_l"))
        listener.on_press(FakeKey(name="space"))  # completes the combo
        hook_returned.set()

    listener.submit(hook_job)

    # The hook callback returned while the handler still waits on the gate:
    # the heavy work never ran on the listener thread...
    assert hook_returned.wait(2.0)
    assert started.wait(2.0)
    assert not gate.is_set()
    # ...and the event reached the worker, on a thread of its own.
    assert handler_threads == ["hold-hotkey-worker"]
    gate.set()
    hold.stop()


def test_hold_worker_queues_a_new_event_while_the_handler_is_slow(
        monkeypatch):
    import threading

    listeners = install_hook_thread_listener(monkeypatch)
    gate = threading.Event()
    press_returned = threading.Event()
    release_returned = threading.Event()
    calls = []

    def slow_press():
        calls.append("press")
        gate.wait(2.0)

    def on_release():
        calls.append("release")

    hold = HoldHotkey("ctrl+space", slow_press, on_release, RecordingLog())
    hold.start()
    listener = listeners[0]

    def press_job():
        listener.on_press(FakeKey(name="ctrl_l"))
        listener.on_press(FakeKey(name="space"))  # down -> worker goes busy
        press_returned.set()

    listener.submit(press_job)
    assert press_returned.wait(2.0)
    assert wait_until(lambda: calls == ["press"])

    def release_job():
        listener.on_release(FakeKey(name="space"))  # up -> queued, not run
        release_returned.set()

    listener.submit(release_job)
    # The hook thread is free at once; the event waits for the busy worker.
    assert release_returned.wait(2.0)
    assert calls == ["press"]

    gate.set()
    # The queued event runs next: order kept, exactly one call per event.
    assert wait_until(lambda: calls == ["press", "release"])
    hold.stop()


def test_hold_stop_joins_the_worker(monkeypatch):
    install_hook_thread_listener(monkeypatch)
    hold = HoldHotkey("ctrl+space", lambda: None, lambda: None,
                      RecordingLog())
    hold.start()
    assert wait_until(lambda: len(threads_named("hold-hotkey-worker")) == 1)

    hold.stop()

    assert wait_until(lambda: threads_named("hold-hotkey-worker") == [])


def test_hold_worker_survives_a_handler_error(monkeypatch):
    listeners = install_hook_thread_listener(monkeypatch)
    log = RecordingLog()
    calls = []

    def boom():
        raise RuntimeError("handler failed")

    hold = HoldHotkey("ctrl+space", boom, lambda: calls.append("release"),
                      log)
    hold.start()
    listener = listeners[0]

    def press_job():
        listener.on_press(FakeKey(name="ctrl_l"))
        listener.on_press(FakeKey(name="space"))

    listener.submit(press_job)
    assert wait_until(lambda: "Hold hotkey handler error" in log.lines)

    listener.submit(lambda: listener.on_release(FakeKey(name="space")))
    assert wait_until(lambda: calls == ["release"])  # the worker lives on
    hold.stop()


def test_toggle_worker_runs_the_callback_off_the_hook_thread(monkeypatch):
    import queue as queue_mod
    import threading

    created = []

    class FakeGlobalHotKeys(threading.Thread):
        """A GlobalHotKeys stand-in: a real thread running hook callbacks."""

        def __init__(self, mapping):
            super().__init__(daemon=True)
            self.mapping = mapping
            self._jobs = queue_mod.Queue()
            created.append(self)

        def run(self):
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                job()

        def submit(self, fn):
            self._jobs.put(fn)

        def stop(self):
            self._jobs.put(None)

    monkeypatch.setattr(hotkey_mod.keyboard, "GlobalHotKeys",
                        FakeGlobalHotKeys)
    gate = threading.Event()
    started = threading.Event()
    hook_returned = threading.Event()
    handler_threads = []

    def slow_toggle():
        handler_threads.append(threading.current_thread().name)
        started.set()
        gate.wait(2.0)

    hotkey = hotkey_mod.Hotkey("ctrl+alt+space", slow_toggle, RecordingLog())
    hotkey.start()
    listener = created[0]

    def hook_job():
        listener.mapping["<ctrl>+<alt>+<space>"]()  # the pynput callback
        hook_returned.set()

    listener.submit(hook_job)

    assert hook_returned.wait(2.0)   # back before the handler finished
    assert started.wait(2.0)
    assert not gate.is_set()
    assert handler_threads == ["hotkey-worker"]
    gate.set()

    hotkey.stop()
    assert wait_until(lambda: threads_named("hotkey-worker") == [])


# -- combo aliases ---------------------------------------------------------
# A hand-edited config.json carries the names a person types ("control",
# "win", "escape"); pynput only accepts its own canonical key names, so the
# combo has to be normalized before it reaches the listener.


def test_combo_to_pynput_normalizes_hotkey_aliases():
    assert hotkey_mod.combo_to_pynput("control+alt+space") == \
        "<ctrl>+<alt>+<space>"
    assert hotkey_mod.combo_to_pynput("win+escape") == "<cmd>+<esc>"
    assert hotkey_mod.combo_to_pynput("Return") == "<enter>"


import keymap  # noqa: E402 - appended section, the imports above stay as they were


@pytest.mark.parametrize("alias", sorted(keymap.KEY_ALIASES))
def test_every_hotkey_aliases_entry_parses_in_pynput(alias):
    from pynput import keyboard

    combo = hotkey_mod.combo_to_pynput(alias + "+f9")
    assert combo == "<" + keymap.KEY_ALIASES[alias] + ">+<f9>"
    assert keyboard.HotKey.parse(combo)


def test_toggle_listener_takes_a_canonical_combo_for_hotkey_aliases(
        monkeypatch):
    from pynput import keyboard

    created = []

    class StubGlobalHotKeys:
        """Stands in for pynput, which parses the mapping keys on start."""

        def __init__(self, mapping):
            self.mapping = mapping
            self.daemon = False
            created.append(self)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(hotkey_mod.keyboard, "GlobalHotKeys",
                        StubGlobalHotKeys)
    listener = hotkey_mod.Hotkey("control+alt+space", lambda: None,
                                 RecordingLog())
    listener.start()

    assert list(created[0].mapping) == ["<ctrl>+<alt>+<space>"]
    keyboard.HotKey.parse(next(iter(created[0].mapping)))  # the real parser
    listener.stop()
