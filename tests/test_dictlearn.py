"""Dictionary learning: word-level pairs, the repeat threshold, the add path.

Nothing here is applied automatically: a correction is stored, counted,
and only offered; the dictionary changes solely through add_pair() after a
confirmation. Storage failures never break a dictation.
"""
import sys
import types

import dictionary
import dictlearn
from vibeforma.service import Service

WORD = "\u0441\u043b\u043e\u0432\u043e"      # "slovo"
WORD_FIX = "\u0441\u043b\u043e\u0432\u0430"   # "slova"


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


def make_service():
    cfg = {
        "locale": "en",
        "dictionary": [],
        "history": False,
        "notify_text": False,
    }
    svc = Service(cfg, RecordingLog())
    svc._running = True
    return svc


def fake_insert(monkeypatch, copied=None):
    """An insert module that cannot paste: the draft lands in the clipboard."""
    fake = types.ModuleType("insert")
    fake.RESULT_PASTED = "pasted"
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.activate_window = lambda _hwnd: False
    fake.copy_text = lambda text: (copied if copied is not None else []).append(text)
    monkeypatch.setitem(sys.modules, "insert", fake)


# -- word-level diff ---------------------------------------------------------
def test_a_replaced_word_yields_one_pair():
    assert dictlearn.diff_pairs("hello wrld today", "hello world today") == [
        ("wrld", "world")]


def test_a_replacement_in_the_russian_text_is_a_pair():
    before = f"\u043f\u0440\u0438\u0432\u0435\u0442 {WORD} \u043c\u0438\u0440"
    after = f"\u043f\u0440\u0438\u0432\u0435\u0442 {WORD_FIX} \u043c\u0438\u0440"
    assert dictlearn.diff_pairs(before, after) == [(WORD, WORD_FIX)]


def test_an_inserted_word_is_not_a_pair():
    assert dictlearn.diff_pairs("hello world", "hello big world") == []


def test_a_deleted_word_is_not_a_pair():
    assert dictlearn.diff_pairs("hello big world", "hello world") == []


def test_unchanged_text_learns_nothing():
    assert dictlearn.diff_pairs("hello world", "hello world") == []
    assert dictlearn.diff_pairs("", "") == []


def test_the_prefill_is_one_short_line():
    assert dictlearn._short("  hello \n world  ") == "hello world"
    assert len(dictlearn._short("x" * 200)) == dictionary.MAX_FROM


# -- the store and the threshold ---------------------------------------------
def test_a_repeated_pair_reaches_the_threshold(tmp_path):
    path = str(tmp_path / "corrections.json")
    assert dictlearn.suggestions(path) == []

    dictlearn.collect_correction("hello wrld", "hello world", path)
    assert dictlearn.suggestions(path) == []          # one repeat is silent

    dictlearn.collect_correction("bye wrld", "bye world", path)
    rows = dictlearn.suggestions(path)
    assert len(rows) == 1
    assert rows[0]["from"] == "wrld"
    assert rows[0]["to"] == "world"
    assert rows[0]["count"] == 2


def test_suggestions_put_the_most_frequent_first(tmp_path):
    path = str(tmp_path / "corrections.json")
    for text in ("a wrld", "b wrld", "c wrld"):
        dictlearn.collect_correction(text, text.replace("wrld", "world"), path)
    for text in ("a wrng", "b wrng"):
        dictlearn.collect_correction(text, text.replace("wrng", "wrong"), path)

    rows = dictlearn.suggestions(path)

    assert [row["from"] for row in rows] == ["wrld", "wrng"]
    assert [row["count"] for row in rows] == [3, 2]


def test_a_broken_store_reads_empty_and_keeps_working(tmp_path):
    path = tmp_path / "corrections.json"
    path.write_text("{not json", encoding="utf-8")
    assert dictlearn.suggestions(str(path)) == []

    dictlearn.collect_correction("hello wrld", "hello world", str(path))
    dictlearn.collect_correction("bye wrld", "bye world", str(path))
    rows = dictlearn.suggestions(str(path))
    assert rows and rows[0]["from"] == "wrld" and rows[0]["count"] == 2


def test_frequency_weights_the_target_forms(tmp_path):
    path = str(tmp_path / "corrections.json")
    dictlearn.collect_correction("a wrld", "a world", path)
    dictlearn.collect_correction("b wrld", "b world", path)
    dictlearn.collect_correction("a wrng", "a wrong", path)

    assert dictlearn.frequency(path) == {"world": 2, "wrong": 1}


def test_frequency_of_a_broken_store_is_empty(tmp_path):
    path = tmp_path / "corrections.json"
    path.write_text("{not json", encoding="utf-8")

    assert dictlearn.frequency(str(path)) == {}


def test_junk_rows_are_ignored(tmp_path):
    path = tmp_path / "corrections.json"
    path.write_text(
        '{"pairs": [1, "x", {"from": "a", "to": "a"}, '
        '{"from": "wrld", "to": "world", "count": 2}]}',
        encoding="utf-8")
    rows = dictlearn.suggestions(str(path))
    assert rows == [{"from": "wrld", "to": "world", "count": 2}]


# -- add_pair: the one place a confirmed pair enters the dictionary ----------
def test_add_pair_updates_cfg_and_saves():
    calls = []
    cfg = {"dictionary": [{"from": "vibra", "to": "VibeForma"}]}

    assert dictlearn.add_pair(cfg, lambda: calls.append(1), " wrld ", " world ")

    assert cfg["dictionary"] == [
        {"from": "vibra", "to": "VibeForma"},
        {"from": "wrld", "to": "world"},
    ]
    assert calls == [1]


def test_add_pair_does_not_duplicate_the_same_from():
    cfg = {"dictionary": []}
    dictlearn.add_pair(cfg, None, "wrld", "world")
    dictlearn.add_pair(cfg, None, "WRLD", "world")

    assert cfg["dictionary"] == [{"from": "wrld", "to": "world"}]


def test_add_pair_refuses_empty_or_identical_values():
    calls = []
    cfg = {"dictionary": []}

    assert not dictlearn.add_pair(cfg, lambda: calls.append(1), "word", "word")
    assert not dictlearn.add_pair(cfg, lambda: calls.append(1), "", "x")
    assert not dictlearn.add_pair(cfg, lambda: calls.append(1), "x", "")

    assert cfg["dictionary"] == []
    assert calls == []


# -- nothing is applied automatically ----------------------------------------
def test_learning_never_touches_the_dictionary(tmp_path):
    cfg = {"dictionary": []}
    path = str(tmp_path / "corrections.json")

    dictlearn.collect_correction("hello wrld", "hello world", path)
    dictlearn.collect_correction("hello wrld", "hello world", path)

    assert cfg["dictionary"] == []
    text, hits = dictionary.apply("hello wrld", cfg["dictionary"])
    assert (text, hits) == ("hello wrld", 0)


# -- the service compares the draft it showed with the text it inserts -------
def test_insert_draft_learns_the_edited_draft(monkeypatch):
    svc = make_service()
    svc.live["draft"] = "hello wrld"
    fake_insert(monkeypatch)
    seen = []
    monkeypatch.setattr(
        dictlearn, "collect_correction",
        lambda original, edited: seen.append((original, edited)) or [])

    svc._insert_draft_worker("hello world", None, "hello wrld")

    assert seen == [("hello wrld", "hello world")]


def test_insert_draft_keeps_an_unchanged_draft_out_of_the_store(monkeypatch):
    svc = make_service()
    svc.live["draft"] = "hello world"
    fake_insert(monkeypatch)
    seen = []
    monkeypatch.setattr(
        dictlearn, "collect_correction",
        lambda original, edited: seen.append((original, edited)) or [])

    svc._insert_draft_worker("hello world", None, "hello world")

    assert seen == []


def test_a_broken_correction_store_does_not_break_insertion(
        monkeypatch, tmp_path):
    svc = make_service()
    svc.live["draft"] = "hello wrld"
    # corrections.json is a directory: reading and writing both fail
    (tmp_path / "corrections.json").mkdir()
    monkeypatch.setattr(dictlearn, "DATA_DIR", str(tmp_path))
    copied = []
    fake_insert(monkeypatch, copied)

    svc._insert_draft_worker("hello world", None, "hello wrld")

    assert copied == ["hello world"]
    assert svc.live["draft"] == ""
    assert svc.live["last"] == (
        "No insertion point \u2014 the text is in the clipboard")
