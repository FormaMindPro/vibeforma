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


class KeyCode:
    @staticmethod
    def from_vk(vk):
        return ("vk", vk)


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
keyboard.KeyCode = KeyCode
keyboard.Controller = Controller
pynput = types.ModuleType("pynput")
pynput.keyboard = keyboard
sys.modules["pynput"] = pynput
sys.modules["pynput.keyboard"] = keyboard

import insert  # noqa: E402

TARGET = 7
OTHER = 8


class ImmediateThread:
    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


@pytest.fixture(autouse=True)
def fast_paths(monkeypatch):
    pyperclip.clip = "old"
    Controller.last = None
    monkeypatch.setattr(insert.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(insert, "foreground_window", lambda: "editor")
    monkeypatch.setattr(insert, "get_foreground_hwnd", lambda: TARGET)
    monkeypatch.setattr(insert, "clipboard_sequence", lambda: 3)
    monkeypatch.setattr(insert, "exclude_from_clipboard_history", lambda: True)
    # The machine may hold a real modifier while the tests run; the physical
    # key state is never what a unit test is about.
    monkeypatch.setattr(insert, "modifier_down", lambda: False)


def test_shift_insert_pastes_and_leaves_the_text():
    result = insert.insert_text("hello", restore_clipboard=False,
                                paste_method="shift_insert")

    assert result == insert.RESULT_PASTED
    assert pyperclip.clip == "hello"
    assert Controller.last.events == [
        ("press", "shift"), ("press", "insert"),
        ("release", "insert"), ("release", "shift"),
    ]


def test_ctrl_v_pastes():
    insert.insert_text("hello", restore_clipboard=False, paste_method="ctrl_v")

    assert pyperclip.clip == "hello"
    assert Controller.last.events == [
        ("press", "ctrl"), ("press", "v"),
        ("release", "v"), ("release", "ctrl"),
    ]


def test_previous_clipboard_is_restored(monkeypatch):
    monkeypatch.setattr(insert.threading, "Thread", ImmediateThread)

    insert.insert_text("hello", restore_clipboard=True,
                       paste_method="shift_insert")

    assert pyperclip.clip == "old"


def test_empty_previous_clipboard_is_restored(monkeypatch):
    pyperclip.clip = ""
    monkeypatch.setattr(insert.threading, "Thread", ImmediateThread)

    insert.insert_text("hello", restore_clipboard=True)

    assert pyperclip.clip == ""


def test_unreadable_clipboard_still_pastes(monkeypatch):
    def broken_paste():
        raise OSError("clipboard busy")

    monkeypatch.setattr(pyperclip, "paste", broken_paste)

    insert.insert_text("hello", restore_clipboard=True)

    assert pyperclip.clip == "hello"
    assert Controller.last.events


def test_focus_change_copies_instead_of_pasting(monkeypatch):
    monkeypatch.setattr(insert, "get_foreground_hwnd", lambda: OTHER)

    result = insert.insert_text("hello", restore_clipboard=True,
                                target_hwnd=TARGET)

    assert result == insert.RESULT_COPIED
    assert Controller.last is None
    assert pyperclip.clip == "hello"   # left for the user to paste


def test_same_window_pastes_directly(monkeypatch):
    result = insert.insert_text("hello", restore_clipboard=False,
                                target_hwnd=TARGET)

    assert result == insert.RESULT_PASTED
    assert Controller.last.events


def test_user_copy_during_delay_is_kept(monkeypatch):
    calls = {"n": 0}

    def sequence():
        calls["n"] += 1
        if calls["n"] >= 2:
            pyperclip.clip = "user copy"
            return 4      # the clipboard changed after our own copy
        return 3

    monkeypatch.setattr(insert, "clipboard_sequence", sequence)
    monkeypatch.setattr(insert.threading, "Thread", ImmediateThread)

    insert.insert_text("hello", restore_clipboard=True)

    assert pyperclip.clip == "user copy"


def test_history_exclusion_runs_after_the_copy(monkeypatch):
    seen = []

    def mark():
        seen.append(pyperclip.clip)
        return True

    monkeypatch.setattr(insert, "exclude_from_clipboard_history", mark)

    insert.insert_text("hello", restore_clipboard=False)

    assert seen == ["hello"]


def test_history_exclusion_can_be_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(insert, "exclude_from_clipboard_history",
                        lambda: calls.append(1) or True)

    insert.insert_text("hello", restore_clipboard=False, exclude_history=False)

    assert calls == []


# -- the held combo (measured 2026-11-26) ----------------------------------

def test_paste_waits_until_the_combo_is_released(monkeypatch):
    """A held Ctrl+Alt turns Shift+Insert into a shortcut no app pastes."""
    held = [True, True, False]
    seen = []

    def modifier_down():
        seen.append(1)
        return held.pop(0) if held else False

    monkeypatch.setattr(insert, "modifier_down", modifier_down)

    result = insert.insert_text("hello", restore_clipboard=False)

    assert result == insert.RESULT_PASTED
    assert len(seen) >= 3          # it waited, then pasted
    assert Controller.last.events == [
        ("press", "shift"), ("press", "insert"),
        ("release", "insert"), ("release", "shift"),
    ]


def test_paste_goes_ahead_after_the_wait_times_out(monkeypatch):
    monkeypatch.setattr(insert, "modifier_down", lambda: True)
    monkeypatch.setattr(insert, "MODIFIER_WAIT_S", 0.0)
    log = RecordingLog()

    insert.insert_text("hello", restore_clipboard=False, log=log)

    assert Controller.last.events
    assert "Modifiers still held after 0.0 s — pasting anyway" in log.lines


def test_the_text_stays_on_the_clipboard_when_the_shortcut_may_miss(monkeypatch):
    monkeypatch.setattr(insert, "modifier_down", lambda: True)
    monkeypatch.setattr(insert, "MODIFIER_WAIT_S", 0.0)
    monkeypatch.setattr(insert.threading, "Thread", ImmediateThread)
    log = RecordingLog()

    insert.insert_text("hello", restore_clipboard=True, log=log)

    assert pyperclip.clip == "hello"
    assert "Paste may have missed — the text stays in the clipboard" in log.lines


def test_the_restore_compares_against_the_state_our_own_writes_left(monkeypatch):
    """The history exclusion writes to the clipboard too; that write is ours."""
    seq = {"n": 3}

    def exclude():
        seq["n"] += 1      # registering the private formats bumps the counter
        return True

    monkeypatch.setattr(insert, "exclude_from_clipboard_history", exclude)
    monkeypatch.setattr(insert, "clipboard_sequence", lambda: seq["n"])
    monkeypatch.setattr(insert.threading, "Thread", ImmediateThread)

    insert.insert_text("hello", restore_clipboard=True)

    assert pyperclip.clip == "old"


class RecordingLog:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)


def test_window_title_is_not_logged_by_default(monkeypatch):
    def forbidden():
        raise AssertionError("the window title must not be read")

    monkeypatch.setattr(insert, "foreground_window", forbidden)
    log = RecordingLog()

    insert.insert_text("hello", restore_clipboard=False, log=log)

    assert log.lines == ["Pasted 5 chars"]


def test_window_title_is_logged_on_opt_in():
    log = RecordingLog()

    insert.insert_text("hello", restore_clipboard=False, log=log,
                       log_title=True)

    assert log.lines == ["Pasted 5 chars — window: editor"]


def test_undo_state_receives_the_previous_clipboard():
    state = {}

    insert.insert_text("hello", restore_clipboard=False, undo_state=state)

    assert state == {"prev": "old", "prev_known": True}


def test_undo_state_marks_an_unreadable_clipboard(monkeypatch):
    def broken_paste():
        raise OSError("clipboard busy")

    monkeypatch.setattr(pyperclip, "paste", broken_paste)
    state = {}

    insert.insert_text("hello", restore_clipboard=False, undo_state=state)

    assert state == {"prev": None, "prev_known": False}


def test_send_undo_presses_the_physical_z_key():
    insert.send_undo()

    assert Controller.last.events == [
        ("press", "ctrl"), ("press", ("vk", insert.UNDO_VK)),
        ("release", ("vk", insert.UNDO_VK)), ("release", "ctrl"),
    ]


def test_copy_text_writes_the_clipboard():
    insert.copy_text("hello")

    assert pyperclip.clip == "hello"


class FakeFunc:
    def __init__(self, impl):
        self._impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._impl(*args)


class FakeUser32:
    def __init__(self):
        self.iconic = 0
        self.calls = []
        self.GetForegroundWindow = FakeFunc(lambda: TARGET)
        self.ShowWindow = FakeFunc(
            lambda hwnd, command: self.calls.append(("show", command)) or 1)
        self.SetForegroundWindow = FakeFunc(
            lambda hwnd: self.calls.append(("fg", hwnd)) or 1)
        self.IsIconic = FakeFunc(lambda hwnd: self.iconic)


def _fake_ctypes(user32):
    return types.SimpleNamespace(
        windll=types.SimpleNamespace(user32=user32),
        c_void_p=lambda value: value,
        c_int=int,
        c_uint=int,
        c_ulong=int,
    )


def test_activate_window_does_not_unmaximize(monkeypatch):
    user32 = FakeUser32()
    monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(user32))
    monkeypatch.setattr(insert.time, "sleep", lambda _seconds: None)

    assert insert.activate_window(TARGET) is True

    assert ("show", 9) not in user32.calls
    assert ("fg", TARGET) in user32.calls


def test_activate_window_restores_a_minimized_window(monkeypatch):
    user32 = FakeUser32()
    user32.iconic = 1
    monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(user32))
    monkeypatch.setattr(insert.time, "sleep", lambda _seconds: None)

    assert insert.activate_window(TARGET) is True

    assert ("show", 9) in user32.calls
    assert ("fg", TARGET) in user32.calls


# -- the selection read (dictation context, F11) ---------------------------

class _CopyingController(Controller):
    """The target app copies the selection on Ctrl+Insert, as Windows does."""

    state = None
    copied = None

    def release(self, key):
        super().release(key)
        if key == "insert" and not self.copied:
            self.copied = True
            pyperclip.clip = self.state["selection"]
            self.state["seq"] += 1


class _QuietController(Controller):
    """An app that ignores Ctrl+Insert: the clipboard does not change."""


def test_selected_text_is_read_and_the_clipboard_put_back(monkeypatch):
    state = {"seq": 1, "selection": "VibeForma"}
    _CopyingController.state = state
    _CopyingController.copied = False
    monkeypatch.setattr(insert, "Controller", _CopyingController)
    monkeypatch.setattr(insert, "clipboard_sequence", lambda: state["seq"])
    pyperclip.clip = "old"

    assert insert.read_selected_text() == "VibeForma"
    assert pyperclip.clip == "old"


def test_no_selection_leaves_the_clipboard_alone(monkeypatch):
    monkeypatch.setattr(insert, "Controller", _QuietController)
    monkeypatch.setattr(insert, "clipboard_sequence", lambda: 1)
    pyperclip.clip = "old"

    assert insert.read_selected_text() == ""
    assert pyperclip.clip == "old"


def test_a_multiline_selection_is_flattened_and_bounded(monkeypatch):
    state = {"seq": 1, "selection": "first line\nsecond line " + "x" * 500}
    _CopyingController.state = state
    _CopyingController.copied = False
    monkeypatch.setattr(insert, "Controller", _CopyingController)
    monkeypatch.setattr(insert, "clipboard_sequence", lambda: state["seq"])
    pyperclip.clip = "old"

    text = insert.read_selected_text()

    assert "\n" not in text
    assert len(text) <= insert.SELECTION_MAX_CHARS
    assert text.startswith("first line second line")


def test_selected_text_waits_for_the_combo_release(monkeypatch):
    """The take is opened with the combo; Ctrl+Alt+Insert would copy nothing."""
    order = []

    class _RecordingController(_QuietController):
        def __init__(self):
            super().__init__()
            order.append("keys")

    def wait(*_args, **_kwargs):
        order.append("wait")
        return True

    monkeypatch.setattr(insert, "wait_for_modifiers_released", wait)
    monkeypatch.setattr(insert, "Controller", _RecordingController)
    monkeypatch.setattr(insert, "clipboard_sequence", lambda: 1)
    pyperclip.clip = "old"

    assert insert.read_selected_text() == ""
    assert order == ["wait", "keys"]


def test_send_undo_waits_for_the_combo_release(monkeypatch):
    order = []

    def wait(*_args, **_kwargs):
        order.append("wait")
        return True

    monkeypatch.setattr(insert, "wait_for_modifiers_released", wait)

    insert.send_undo()

    assert order == ["wait"]
    assert Controller.last.events == [
        ("press", "ctrl"), ("press", ("vk", insert.UNDO_VK)),
        ("release", ("vk", insert.UNDO_VK)), ("release", "ctrl"),
    ]
