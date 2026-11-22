"""History filters: the record direction and the pure list filter.

No Tk here: the table's rules live in history.py as plain functions, so
they are checked without a display.
"""
import datetime
import json
import sys
import types

import history
from vibeforma.service import Service

BASE = datetime.date(2026, 11, 22)
NOW = datetime.datetime(BASE.year, BASE.month, BASE.day, 12, 0).timestamp()


def at(days_ago, hour=12, minute=0):
    """Local epoch for a day counted back from the fixed 'today'."""
    day = BASE - datetime.timedelta(days=days_ago)
    return datetime.datetime(day.year, day.month, day.day,
                             hour, minute).timestamp()


def texts(records):
    return [record["text"] for record in records]


class FakeLog:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class FakeStore:
    def __init__(self):
        self.calls = []

    def add(self, text, app="", way="insert"):
        self.calls.append((text, app, way))

    def load(self):
        return []


def make_service(enabled=True):
    svc = Service.__new__(Service)
    svc.cfg = {"locale": "en", "history": enabled, "history_app": False}
    svc.history = FakeStore()
    svc._target_title = "Editor"
    svc.log = FakeLog()
    svc.live = {"last": "", "draft": "", "draft_rev": 0}
    svc.tray = None
    return svc


def test_old_records_read_as_insert():
    assert history.record_way({"text": "old"}) == history.WAY_INSERT
    assert history.record_way(
        {"text": "old", "way": "copy"}) == history.WAY_COPY
    assert history.record_way(
        {"text": "old", "way": "bogus"}) == history.WAY_INSERT


def test_add_stores_the_way(tmp_path):
    store = history.History(str(tmp_path / "history.jsonl"))
    store.add("first")
    store.add("second", app="Editor", way="copy")

    records = store.load()

    assert records[0]["way"] == history.WAY_INSERT
    assert records[1]["way"] == history.WAY_COPY
    assert records[1]["app"] == "Editor"


def test_a_file_record_without_way_is_an_insert(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(
        json.dumps({"ts": NOW, "text": "old", "chars": 3}) + "\n",
        encoding="utf-8")

    records = history.History(str(path)).load()

    assert history.record_way(records[0]) == history.WAY_INSERT
    assert texts(history.records_matching(
        records, way="insert", now=NOW)) == ["old"]
    assert history.records_matching(records, way="copy", now=NOW) == []


def test_app_filter_is_exact_and_empty_means_all():
    records = [
        {"ts": NOW, "text": "a", "app": "Editor"},
        {"ts": NOW, "text": "b", "app": "Browser"},
        {"ts": NOW, "text": "c"},
    ]

    assert texts(history.records_matching(records, app="Editor")) == ["a"]
    assert history.records_matching(records, app="editor") == []
    assert texts(history.records_matching(
        records, app="")) == ["a", "b", "c"]


def test_today_keeps_only_the_local_date():
    records = [
        {"ts": at(0, 0, 1), "text": "today"},
        {"ts": at(1, 23, 59), "text": "yesterday"},
    ]

    assert texts(history.records_matching(records, days=1,
                                          now=NOW)) == ["today"]


def test_seven_days_include_today_and_six_before():
    records = [
        {"ts": at(0, 0, 1), "text": "today"},
        {"ts": at(6, 0, 1), "text": "six days"},
        {"ts": at(7, 23, 59), "text": "seven days"},
    ]

    assert texts(history.records_matching(records, days=7, now=NOW)) == [
        "today", "six days"]


def test_thirty_days_include_today_and_twenty_nine_before():
    records = [
        {"ts": at(0, 0, 1), "text": "today"},
        {"ts": at(29, 0, 1), "text": "twenty-nine days"},
        {"ts": at(30, 23, 59), "text": "thirty days"},
    ]

    assert texts(history.records_matching(records, days=30, now=NOW)) == [
        "today", "twenty-nine days"]


def test_a_broken_timestamp_fails_the_date_filter():
    records = [{"text": "no stamp"}]

    assert history.records_matching(records, days=7, now=NOW) == []


def test_way_filter_splits_inserts_and_copies():
    records = [
        {"ts": NOW, "text": "a"},
        {"ts": NOW, "text": "b", "way": "copy"},
        {"ts": NOW, "text": "c", "way": "insert"},
    ]

    assert texts(history.records_matching(records, way="copy")) == ["b"]
    assert texts(history.records_matching(
        records, way="insert")) == ["a", "c"]
    assert len(history.records_matching(records, way="")) == 3


def test_query_combines_with_app_date_and_way():
    records = [
        {"ts": at(0, 9), "text": "table draft", "app": "ZCode",
         "way": "insert"},
        {"ts": at(1, 9), "text": "table ready", "app": "ZCode",
         "way": "copy"},
        {"ts": at(1, 9), "text": "other ready", "app": "ZCode",
         "way": "copy"},
        {"ts": at(1, 9), "text": "table elsewhere", "app": "Editor",
         "way": "copy"},
        {"ts": at(10, 9), "text": "table old", "app": "ZCode",
         "way": "copy"},
    ]

    got = history.records_matching(records, query="TABLE", app="ZCode",
                                   days=7, way="copy", now=NOW)

    assert texts(got) == ["table ready"]


def test_empty_filters_return_everything():
    records = [
        {"ts": at(100), "text": "old", "app": "Editor"},
        {"ts": at(0), "text": "new", "app": "Browser", "way": "copy"},
    ]

    assert history.records_matching(records) == records
    assert history.records_matching(records, query="", app="", days=0,
                                    way="", now=NOW) == records


def test_history_filters_judges_one_record_without_tk():
    record = {"ts": at(0, 9), "text": "hello", "app": "Editor",
              "way": "copy"}

    assert history.history_filters(record, query="hell", way="copy",
                                   now=NOW)
    assert not history.history_filters(record, way="insert", now=NOW)
    assert not history.history_filters(record, days=1, now=at(1, 12))


def test_remember_defaults_to_insert_and_accepts_copy():
    svc = make_service()

    svc._remember("one")
    svc._remember("two", way="copy")

    assert svc.history.calls == [("one", "", "insert"),
                                 ("two", "", "copy")]


def test_remember_stays_silent_when_history_is_off():
    svc = make_service(enabled=False)

    svc._remember("one", way="copy")

    assert svc.history.calls == []


def test_copy_draft_remembers_the_copy(monkeypatch):
    svc = make_service()
    fake = types.ModuleType("insert")
    fake.copy_text = lambda _text: None
    fake.exclude_from_clipboard_history = lambda: True
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc.copy_draft(" hello ")

    assert svc.history.calls == [("hello", "", "copy")]


def test_copying_from_the_table_adds_no_entry(monkeypatch):
    svc = make_service()
    fake = types.ModuleType("insert")
    fake.copy_text = lambda _text: None
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc.history_copy("hello")

    assert svc.history.calls == []
