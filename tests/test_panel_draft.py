"""Panel dictation: the recognized text waits in the HUD, never auto-pastes."""
import sys
import types

import hud
from vibeforma.service import Service


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


class FakeRecorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class FakeASR:
    def transcribe(self, _buf):
        return "hello panel"


def make_service():
    cfg = {
        "locale": "en",
        "dictionary": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
    }
    svc = Service(cfg, RecordingLog())
    svc._running = True
    return svc


def test_panel_recording_leaves_the_text_in_the_draft():
    svc = make_service()
    svc.asr = FakeASR()
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.live["draft"] == "hello panel"
    assert svc.live["draft_rev"] == 1
    assert svc.live["state"] == "idle"
    assert svc._panel_mode is False


def test_focus_change_keeps_the_text_in_the_panel(monkeypatch):
    svc = make_service()
    svc.asr = FakeASR()
    svc.state = "processing"
    svc._target_hwnd = 7
    calls = []
    fake = types.ModuleType("insert")
    fake.get_foreground_hwnd = lambda: 8
    fake.insert_text = lambda *args, **kwargs: calls.append("insert_text")
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.live["draft"] == "hello panel"
    assert svc.live["draft_rev"] == 1
    assert svc.live["state"] == "idle"
    assert calls == []


def test_focus_change_with_confirmation_off_copies_instead(monkeypatch):
    svc = make_service()
    svc.asr = FakeASR()
    svc.state = "processing"
    svc._target_hwnd = 7
    svc.cfg["confirm_focus_change"] = False
    calls = []
    fake = types.ModuleType("insert")
    fake.get_foreground_hwnd = lambda: 8
    fake.RESULT_COPIED = "copied"
    fake.insert_text = lambda *args, **kwargs: (
        calls.append(kwargs.get("target_hwnd")) or "copied")
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc._transcribe_worker(object(), FakeRecorder())

    assert calls == [7]
    assert svc.live["draft"] == ""


def test_mic_button_marks_the_next_recording_as_a_panel_one():
    svc = make_service()
    svc.state = "idle"
    calls = []
    svc.start_recording = lambda: calls.append(("start", svc._panel_mode))

    svc.on_panel_mic()

    assert calls == [("start", True)]


def test_mic_button_stops_a_running_recording():
    svc = make_service()
    svc.state = "recording"
    calls = []
    svc.stop_recording = lambda reason: calls.append(reason)

    svc.on_panel_mic()

    assert calls == ["panel"]


def test_cancel_clears_the_draft():
    svc = make_service()
    svc.live["draft"] = "hello"
    svc.live["draft_rev"] = 1

    svc.cancel_draft()

    assert svc.live["draft"] == ""
    assert svc.live["draft_rev"] == 2
    assert svc.live["last"] == "(draft cancelled)"


def test_insert_without_a_target_copies_the_text(monkeypatch):
    svc = make_service()
    svc.live["draft"] = "hello"
    svc.live["draft_rev"] = 1
    copied = []
    fake = types.ModuleType("insert")
    fake.copy_text = lambda text: copied.append(text)
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc._insert_draft_worker("hello", None)

    assert copied == ["hello"]
    assert svc.live["draft"] == ""
    assert svc.live["last"] == "No insertion point — the text is in the clipboard"


def test_copy_draft_puts_the_text_in_the_clipboard(monkeypatch):
    svc = make_service()
    svc.live["draft"] = "hello"
    svc.live["draft_rev"] = 1
    copied = []
    excluded = []
    fake = types.ModuleType("insert")
    fake.copy_text = lambda text: copied.append(text)
    fake.exclude_from_clipboard_history = (
        lambda: excluded.append(1) or True)
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc.copy_draft(" hello ")

    assert copied == ["hello"]
    assert excluded == [1]
    assert svc.live["draft"] == ""
    assert svc.live["last"] == "Copied to the clipboard"


def make_history_service(records):
    svc = make_service()
    svc.cfg["history"] = True
    svc.state = "idle"
    svc.history_records = lambda: [dict(record) for record in records]
    return svc


def test_history_arrows_go_older_and_back_to_the_live_draft():
    svc = make_history_service([{"text": "old"}, {"text": "new"}])
    svc.live["draft"] = "live"
    svc.live["draft_rev"] = 1

    svc.browse_history(-1)
    assert svc.live["draft"] == "new"
    assert svc.live["last"] == "History: 1 of 2"

    svc.browse_history(-1)
    assert svc.live["draft"] == "old"
    assert svc.live["last"] == "History: 2 of 2"

    svc.browse_history(-1)
    assert svc.live["draft"] == "old"
    assert svc.live["last"] == "This is the oldest entry"

    svc.browse_history(1)
    assert svc.live["draft"] == "new"

    svc.browse_history(1)
    assert svc.live["draft"] == "live"
    assert svc.live["last"] == "Text ready — review and insert"


def test_history_cancel_returns_the_set_aside_draft():
    svc = make_history_service([{"text": "old"}])
    svc.live["draft"] = "live"
    svc.live["draft_rev"] = 1

    svc.browse_history(-1)
    assert svc.live["draft"] == "old"

    svc.cancel_draft()
    assert svc.live["draft"] == "live"
    assert svc.live["last"] == "Text ready — review and insert"


def test_newer_past_the_newest_closes_the_history():
    svc = make_history_service([{"text": "old"}])

    svc.browse_history(-1)
    svc.browse_history(1)

    assert svc.live["draft"] == ""
    assert svc.live["last"] == "(history closed)"


def test_history_off_is_reported():
    svc = make_service()
    svc.state = "idle"

    svc.browse_history(-1)

    assert svc.live["last"] == "History is off — enable it in settings"


def test_empty_history_is_reported():
    svc = make_history_service([])

    svc.browse_history(-1)

    assert svc.live["last"] == "No entries yet."


def test_new_recording_clears_the_history_draft():
    svc = make_history_service([{"text": "old"}])

    svc.browse_history(-1)
    assert svc.live["draft"] == "old"

    svc._forget_draft()

    assert svc.live["draft"] == ""
    assert svc._history_cursor is None


def test_hotkey_from_the_panel_uses_the_panel(monkeypatch):
    svc = make_service()
    svc.state = "idle"
    fake = types.ModuleType("insert")
    fake.get_foreground_hwnd = lambda: 7
    fake.is_own_window = lambda hwnd: True
    monkeypatch.setitem(sys.modules, "insert", fake)
    calls = []
    svc.start_recording = lambda: calls.append(svc._panel_mode)

    svc.on_hotkey()

    assert calls == [True]


def test_hotkey_from_another_window_pastes_directly(monkeypatch):
    svc = make_service()
    svc.state = "idle"
    fake = types.ModuleType("insert")
    fake.get_foreground_hwnd = lambda: 7
    fake.is_own_window = lambda hwnd: False
    monkeypatch.setitem(sys.modules, "insert", fake)
    calls = []
    svc.start_recording = lambda: calls.append(svc._panel_mode)

    svc.on_hotkey()

    assert calls == [False]


class FakeText:
    """Just the Text widget surface the draft methods use."""

    def __init__(self, value=""):
        self.value = value
        self.state = "normal"
        self.height = hud.BASE_TEXT_LINES
        self.tags = {}

    def configure(self, **kw):
        if "state" in kw:
            self.state = kw["state"]
        if "height" in kw:
            self.height = kw["height"]

    def cget(self, key):
        return self.height if key == "height" else None

    def get(self, *_args):
        return self.value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, text):
        self.value = text

    def tag_configure(self, *_args, **_kw):
        pass

    def tag_remove(self, tag, *_args):
        self.tags.pop(tag, None)

    def tag_add(self, tag, start, end):
        self.tags.setdefault(tag, []).append((start, end))

    def focus_set(self):
        pass

    def mark_set(self, *_args):
        pass

    def see(self, *_args):
        pass


class FakeRoot:
    def focus_force(self):
        pass


class FakeDraftSvc:
    def __init__(self):
        self.inserted = []
        self.copied = []
        self.cancelled = 0

    def insert_draft(self, text, target_hwnd=None):
        self.inserted.append((text, target_hwnd))

    def copy_draft(self, text):
        self.copied.append(text)

    def cancel_draft(self):
        self.cancelled += 1


def make_hud(text="hello"):
    """A Hud with only the widgets and service calls the draft methods touch."""
    h = hud.Hud.__new__(hud.Hud)
    h.cfg = {}
    h.log = RecordingLog()
    h.text = FakeText(text)
    h.root = FakeRoot()
    h.svc = FakeDraftSvc()
    h._draft_active = True
    h._draft_edit = False
    h._draft_text = text
    h._draft_rev = 0
    h._last_foreign_hwnd = None
    h._want = hud.BASE_TEXT_LINES
    h._edit_label = False
    h._actions_enabled = True
    h._hidden = False
    h._set_edit_label = lambda editing: setattr(h, "_edit_label", editing)
    h._set_actions_enabled = lambda enabled: setattr(
        h, "_actions_enabled", enabled)
    h._show_actions = lambda: None
    h._hide_panel = lambda: setattr(h, "_hidden", True)
    h._save_edit = lambda: hud.Hud._save_edit(h)
    h._clear_draft = lambda: setattr(h, "_draft_active", False)
    h._track_foreign_window = lambda: None
    h._apply_height = lambda: None
    h._display_lines = lambda: 0
    return h


def test_draft_autogrow_keeps_a_short_draft_at_the_minimum():
    h = make_hud("hi")

    hud.Hud._autogrow(h, "hi", draft=True)

    assert h._want == hud.DRAFT_MIN_LINES
    assert h.text.height == hud.DRAFT_MIN_LINES


def test_draft_autogrow_grows_medium_text_line_by_line():
    h = make_hud("")

    hud.Hud._autogrow(h, "\n" * 7, draft=True)

    assert h._want == 8
    assert h.text.height == 8


def test_draft_autogrow_grows_long_text_to_the_cap():
    h = make_hud("")

    hud.Hud._autogrow(h, "word " * 500, draft=True)

    assert h._want == hud.MAX_TEXT_LINES


def test_partial_text_without_a_draft_stays_at_base():
    h = make_hud("")

    hud.Hud._autogrow(h, "hi")

    assert h._want == hud.BASE_TEXT_LINES


def test_autogrow_off_keeps_the_draft_readable_but_the_partial_at_base():
    h = make_hud("")
    h.cfg["hud_autogrow"] = False

    hud.Hud._autogrow(h, "hi", draft=True)
    assert h._want == hud.DRAFT_MIN_LINES

    hud.Hud._autogrow(h, "hi")
    assert h._want == hud.BASE_TEXT_LINES


def test_draft_edit_round_trip_saves_the_corrected_text():
    h = make_hud("hello panel")

    hud.Hud._toggle_edit(h)
    assert h._draft_edit is True
    assert h.text.state == "normal"
    assert h._edit_label is True

    h.text.value = "hello, panel!"
    hud.Hud._toggle_edit(h)
    assert h._draft_edit is False
    assert h._draft_text == "hello, panel!"
    assert h.text.state == "disabled"
    assert h._edit_label is False


def test_escape_while_editing_saves_and_keeps_the_draft():
    h = make_hud("hello")
    hud.Hud._toggle_edit(h)
    h.text.value = "fixed"

    assert hud.Hud._on_escape(h) == "break"

    assert h._draft_edit is False
    assert h._draft_text == "fixed"
    assert h._draft_active is True
    assert h._hidden is False


def test_escape_without_editing_hides_the_panel():
    h = make_hud("hello")

    assert hud.Hud._on_escape(h) == "break"

    assert h._hidden is True
    assert h._draft_text == "hello"


def test_ctrl_enter_inserts_into_the_tracked_window():
    h = make_hud(" hello ")
    h._last_foreign_hwnd = 4242

    assert hud.Hud._on_ctrl_enter(h) == "break"

    assert h.svc.inserted == [("hello", 4242)]
    assert h._actions_enabled is False


def test_ctrl_enter_without_a_target_still_inserts():
    h = make_hud("hello")

    hud.Hud._on_ctrl_enter(h)

    assert h.svc.inserted == [("hello", None)]


def test_new_draft_forgets_the_window_chosen_before_it():
    h = make_hud("")
    h._last_foreign_hwnd = 4242
    h._autogrow = lambda text, draft=False: None

    hud.Hud._show_draft(h, "hello")

    assert h._last_foreign_hwnd is None
    assert h._draft_active is True
    assert h.text.value == "hello"
    assert h.text.state == "disabled"


def test_copy_uses_the_text_from_the_editor():
    h = make_hud("hello")
    hud.Hud._toggle_edit(h)
    h.text.value = "  hello there  "

    hud.Hud._copy_draft(h)

    assert h.svc.copied == ["hello there"]
    assert h._actions_enabled is False


def test_cancel_closes_the_draft_and_tells_the_service():
    h = make_hud("hello")

    hud.Hud._cancel_draft(h)

    assert h.svc.cancelled == 1
    assert h._draft_active is False


def test_a_doubtful_word_is_marked_inside_the_draft():
    h = make_hud("")

    hud.Hud._show_draft(h, "the noisy phrase", ["noisy"])

    assert h.text.value == "the noisy phrase"
    assert h.text.tags[hud.DRAFT_UNCERTAIN_TAG] == [("1.0+4c", "1.0+9c")]


def test_the_live_draft_carries_its_doubtful_words_into_the_panel():
    h = make_hud("")
    st = {"draft_rev": 1, "draft": "a murky phrase",
          "draft_uncertain": ["murky"]}

    hud.Hud._sync_draft(h, st)

    assert h.text.value == "a murky phrase"
    assert h.text.tags[hud.DRAFT_UNCERTAIN_TAG] == [("1.0+2c", "1.0+7c")]


def test_a_confident_draft_carries_no_marks():
    h = make_hud("")

    hud.Hud._show_draft(h, "hello panel")

    assert h.text.tags.get(hud.DRAFT_UNCERTAIN_TAG) is None


def test_insert_from_a_marked_draft_pastes_the_whole_phrase():
    h = make_hud("")

    hud.Hud._show_draft(h, " hi there ", ["hi"])
    hud.Hud._on_ctrl_enter(h)

    assert h.svc.inserted == [("hi there", None)]
