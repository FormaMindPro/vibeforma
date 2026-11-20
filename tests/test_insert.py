"""Insertion with a substituted clipboard and keyboard (no real Windows APIs)."""
import sys
import types

import pytest

pyperclip = types.ModuleType("pyperclip")
pyperclip.clip = "old"


def _paste():
    return pyperclip.clip


def _copy(value):
    pyperclip.clip = value


pyperclip.paste = _paste
pyperclip.copy = _copy
sys.modules["pyperclip"] = pyperclip

keyboard = types.ModuleType("pynput.keyboard")


class Key:
    shift = "shift"
    insert = "insert"
    ctrl = "ctrl"


class Controller:
    last = None

    def __init__(self):
        Controller.last = self
        self.events = []

    def press(self, key):
        self.events.append(("press", key))

    def release(self, key):
        self.events.append(("release", key))

    def pressed(self, key):
        return _Pressed(self, key)


class _Pressed:
    def __init__(self, controller, key):
        self.controller = controller
        self.key = key

    def __enter__(self):
        self.controller.press(self.key)
        return self

    def __exit__(self, *exc):
        self.controller.release(self.key)
        return False


keyboard.Key = Key
keyboard.Controller = Controller
pynput = types.ModuleType("pynput")
pynput.keyboard = keyboard
sys.modules["pynput"] = pynput
sys.modules["pynput.keyboard"] = keyboard

import insert  # noqa: E402


@pytest.fixture(autouse=True)
def fast_paths(monkeypatch):
    pyperclip.clip = "old"
    Controller.last = None
    monkeypatch.setattr(insert.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(insert, "restore_foreground", lambda _hwnd: None)
    monkeypatch.setattr(insert, "foreground_window", lambda: "editor")


def test_shift_insert_pastes_and_leaves_the_text(monkeypatch):
    insert.insert_text("hello", restore_clipboard=False,
                       paste_method="shift_insert")

    assert pyperclip.clip == "hello"
    assert Controller.last.events == [
        ("press", "shift"), ("press", "insert"),
        ("release", "insert"), ("release", "shift"),
    ]


def test_ctrl_v_pastes(monkeypatch):
    insert.insert_text("hello", restore_clipboard=False, paste_method="ctrl_v")

    assert pyperclip.clip == "hello"
    assert Controller.last.events == [
        ("press", "ctrl"), ("press", "v"),
        ("release", "v"), ("release", "ctrl"),
    ]


def test_previous_clipboard_is_restored(monkeypatch):
    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(insert.threading, "Thread", ImmediateThread)

    insert.insert_text("hello", restore_clipboard=True,
                       paste_method="shift_insert")

    assert pyperclip.clip == "old"


def test_unreadable_clipboard_still_pastes(monkeypatch):
    def broken_paste():
        raise OSError("clipboard busy")

    monkeypatch.setattr(pyperclip, "paste", broken_paste)

    insert.insert_text("hello", restore_clipboard=False,
                       paste_method="shift_insert")

    assert pyperclip.clip == "hello"
    assert Controller.last.events
