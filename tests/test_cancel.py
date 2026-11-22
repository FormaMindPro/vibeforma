"""Esc cancels a dictation: the buffer or the recognition is dropped."""
import sys
import threading
import types

import cancel_hotkey
from cancel_hotkey import EscWatcher
from vibeforma import STATE_IDLE, STATE_PROCESSING, STATE_RECORDING
from vibeforma.service import Service

CANCELLED = "Recording cancelled — nothing inserted"


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


class FakeTray:
    def __init__(self):
        self.messages = []
        self.states = []

    def notify(self, msg, title="VibeForma"):
        self.messages.append(msg)

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
        self.stopped = False

    def had_speech(self):
        return True

    def stop(self):
        self.stopped = True
        return object()


class FakeASR:
    def __init__(self, text="hello", on_transcribe=None):
        self.text = text
        self.calls = 0
        self._on_transcribe = on_transcribe

    def transcribe(self, _buf, verbose=True):
        self.calls += 1
        if self._on_transcribe is not None:
            self._on_transcribe()
        return self.text


class FakeHistory:
    def __init__(self):
        self.added = []

    def add(self, text, app="", way="insert"):
        self.added.append(text)

    def load(self):
        return []


def make_service():
    cfg = {
        "locale": "en",
        "dictionary": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": True,
        "history_app": False,
    }
    svc = Service(cfg, RecordingLog())
    svc.history = FakeHistory()
    svc.tray = FakeTray()
    watcher = FakeWatcher()
    svc.attach_cancel_watcher(watcher)
    svc.asr = FakeASR()
    return svc, watcher


def test_esc_while_recording_stops_and_drops_the_buffer():
    svc, watcher = make_service()
    rec = FakeRecorder()
    svc.recorder = rec
    svc.live["partial"] = "half a phrase"
    svc.live["level"] = 0.4
    svc._set_state(STATE_RECORDING)
    assert watcher.active

    svc.cancel_recording()

    assert rec.stopped is True
    assert svc.asr.calls == 0            # nothing was recognized
    assert svc.state == STATE_IDLE
    assert svc.recorder is None
    assert svc.live["partial"] == ""
    assert svc.live["level"] == 0.0
    assert svc.live["last"] == CANCELLED
    assert svc.tray.messages == [CANCELLED]
    assert svc.history.added == []
    assert watcher.active is False
    assert watcher.events == ["start", "stop"]


def test_esc_while_recognizing_drops_the_result(monkeypatch):
    inserted = []

    def forbidden(*args, **kwargs):
        inserted.append(args)
        raise AssertionError("a cancelled result must not be inserted")

    fake = types.ModuleType("insert")
    fake.RESULT_PASTED = "pasted"
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.insert_text = forbidden
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc, watcher = make_service()
    svc._set_state(STATE_RECORDING)
    svc.asr = FakeASR(on_transcribe=svc.cancel_recording)
    svc._set_state(STATE_PROCESSING)
    assert watcher.active               # Esc still works while recognizing

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.asr.calls == 1
    assert inserted == []
    assert svc.state == STATE_IDLE
    assert svc.live["draft"] == ""
    assert svc.live["partial"] == ""
    assert svc.live["level"] == 0.0
    assert svc.history.added == []
    assert svc.tray.messages == [CANCELLED]   # no "inserted" notice
    assert svc._cancel_evt.is_set() is False
    assert watcher.active is False


def test_esc_at_rest_does_nothing():
    svc, watcher = make_service()
    svc._set_state(STATE_IDLE)

    svc.cancel_recording()

    assert svc.state == STATE_IDLE
    assert svc.live["last"] == ""
    assert svc.tray.messages == []
    assert watcher.events == []         # the listener never existed


def test_watcher_follows_the_dictation_state():
    svc, watcher = make_service()

    svc._set_state(STATE_RECORDING)
    assert watcher.active is True
    assert watcher.events == ["start"]

    svc._set_state(STATE_PROCESSING)
    assert watcher.active is True       # recognition stays cancellable

    svc._set_state(STATE_IDLE)
    assert watcher.active is False
    assert watcher.events == ["start", "stop"]


def test_repeated_cancel_is_safe():
    svc, _watcher = make_service()
    rec = FakeRecorder()
    svc.recorder = rec
    svc._set_state(STATE_RECORDING)

    svc.cancel_recording()
    svc.cancel_recording()

    assert rec.stopped is True
    assert svc.state == STATE_IDLE
    assert svc.tray.messages == [CANCELLED]

    svc._set_state(STATE_PROCESSING)
    svc.cancel_recording()
    svc.cancel_recording()

    assert svc._cancel_evt.is_set() is True
    assert svc.tray.messages == [CANCELLED, CANCELLED]


def test_cancel_without_a_recorder_is_safe():
    svc, _watcher = make_service()
    svc.recorder = None
    svc._set_state(STATE_RECORDING)

    svc.cancel_recording()

    assert svc.state == STATE_IDLE
    assert svc.live["last"] == CANCELLED
    assert svc.history.added == []


def test_ordinary_stop_still_inserts_and_saves(monkeypatch):
    calls = []
    fake = types.ModuleType("insert")
    fake.RESULT_PASTED = "pasted"
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.insert_text = lambda text, **kw: calls.append(text) or "pasted"
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc, _watcher = make_service()
    svc._set_state(STATE_RECORDING)
    svc._set_state(STATE_PROCESSING)

    svc._transcribe_worker(object(), FakeRecorder())

    assert calls == ["hello"]
    assert svc.history.added == ["hello"]
    assert svc.state == STATE_IDLE
    assert svc.tray.messages == ["Inserted, 5 chars"]


def test_esc_watcher_creates_a_listener_only_between_start_and_stop(monkeypatch):
    listeners = []

    class FakeListener:
        def __init__(self, on_press=None):
            self.on_press = on_press
            self.daemon = False
            self.started = False
            self.stopped = False
            listeners.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(cancel_hotkey.keyboard, "Listener", FakeListener)
    fired = threading.Event()
    watcher = EscWatcher(lambda: fired.set(), RecordingLog())
    assert watcher.active is False
    assert listeners == []

    watcher.start()
    watcher.start()                     # idempotent: one listener only
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener.started is True
    assert listener.daemon is True

    listener.on_press(cancel_hotkey.keyboard.Key.space)
    assert fired.is_set() is False
    listener.on_press(cancel_hotkey.keyboard.Key.esc)
    assert fired.wait(2)                # the callback runs in its own thread

    watcher.stop()
    watcher.stop()                      # idempotent
    assert listener.stopped is True
    assert watcher.active is False
