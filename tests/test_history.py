"""History: capped JSONL ring, clearing, undo guard, no text in the log."""
import inspect
import json
import sys
import time
import types

import history
import main


def test_entries_survive_a_restart(tmp_path):
    path = str(tmp_path / "history.jsonl")
    first = history.History(path)
    first.add("hello world")

    second = history.History(path)
    records = second.load()
    assert len(records) == 1
    assert records[0]["text"] == "hello world"
    assert records[0]["chars"] == 11


def test_history_keeps_the_last_hundred(tmp_path):
    store = history.History(str(tmp_path / "history.jsonl"))
    for i in range(105):
        store.add(f"text {i:03d}")
    records = store.load()
    assert len(records) == history.MAX_RECORDS
    assert records[0]["text"] == "text 005"
    assert records[-1]["text"] == "text 104"


def test_clear_removes_the_file(tmp_path):
    path = tmp_path / "history.jsonl"
    store = history.History(str(path))
    store.add("hello")
    assert path.exists()

    store.clear()
    assert not path.exists()
    assert store.load() == []


def test_delete_removes_one_entry(tmp_path):
    store = history.History(str(tmp_path / "history.jsonl"))
    store.add("one")
    store.add("two")
    store.delete(0)
    assert [r["text"] for r in store.load()] == ["two"]


def test_broken_lines_are_skipped(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"ts": 1, "text": "ok", "chars": 2}\nnot json\n',
                    encoding="utf-8")
    records = history.History(str(path)).load()
    assert [r["text"] for r in records] == ["ok"]


def test_app_name_is_optional(tmp_path):
    store = history.History(str(tmp_path / "history.jsonl"))
    store.add("hello", app="editor")
    store.add("world")
    records = store.load()
    assert records[0].get("app") == "editor"
    assert "app" not in records[1]


def test_undo_allowed_guards():
    last = {"hwnd": 7, "ts": 100.0}
    assert history.undo_allowed(None, 100.0, 7) == "none"
    assert history.undo_allowed(last, 110.0, 8) == "other_window"
    assert history.undo_allowed(last, 131.0, 7) == "late"
    assert history.undo_allowed(last, 130.0, 7) == "ok"
    assert history.undo_allowed(last, 100.5, 7) == "ok"


def test_history_module_never_logs_the_text():
    source = inspect.getsource(history)
    assert "logging" not in source
    assert ".info(" not in source
    assert ".warning(" not in source


class FakeStore:
    def __init__(self):
        self.calls = []

    def add(self, text, app=""):
        self.calls.append((text, app))

    def load(self):
        return []


def make_service(enabled, app=False, title="editor"):
    svc = main.Service.__new__(main.Service)
    svc.cfg = {"history": enabled, "history_app": app}
    svc.history = FakeStore()
    svc._target_title = title
    svc.log = None
    return svc


def test_remember_writes_only_when_history_is_on():
    off = make_service(False)
    off._remember("hello")
    assert off.history.calls == []

    on = make_service(True)
    on._remember("hello")
    assert on.history.calls == [("hello", "")]


def test_remember_stores_the_app_only_on_request():
    with_app = make_service(True, app=True, title="editor")
    with_app._remember("hello")
    assert with_app.history.calls == [("hello", "editor")]


def test_file_format_is_jsonl(tmp_path):
    path = tmp_path / "history.jsonl"
    store = history.History(str(path))
    store.add("hello")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "hello"


def make_undo_service(monkeypatch, foreground, last):
    calls = []
    fake = types.ModuleType("insert")
    fake.get_foreground_hwnd = lambda: foreground
    fake.send_undo = lambda: calls.append("undo")
    fake.copy_text = lambda value: calls.append(("copy", value))
    monkeypatch.setitem(sys.modules, "insert", fake)
    svc = main.Service.__new__(main.Service)
    svc.cfg = {"locale": "en"}
    svc.log = None
    svc.live = {"last": "", "last_ts": 0.0}
    svc.tray = None
    svc._last_insert = last
    return svc, calls


def test_undo_skips_a_foreign_window(monkeypatch):
    svc, calls = make_undo_service(
        monkeypatch, 99, {"hwnd": 7, "ts": time.time()})
    message = svc.undo_last()
    assert calls == []
    assert message


def test_undo_runs_in_the_same_window(monkeypatch):
    svc, calls = make_undo_service(
        monkeypatch, 7, {"hwnd": 7, "ts": time.time()})
    svc.undo_last()
    assert calls == ["undo"]


def test_undo_skips_a_late_request(monkeypatch):
    svc, calls = make_undo_service(
        monkeypatch, 7, {"hwnd": 7, "ts": time.time() - 31})
    svc.undo_last()
    assert calls == []


def test_restore_puts_the_previous_clipboard_back(monkeypatch):
    svc, calls = make_undo_service(
        monkeypatch, 7, {"hwnd": 7, "ts": time.time(),
                         "prev": "old", "prev_known": True})
    svc.restore_last_clipboard()
    assert calls == [("copy", "old")]
