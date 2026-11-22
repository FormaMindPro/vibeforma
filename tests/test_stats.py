"""Statistics: daily counters, atomic storage and the service hook (card-182)."""
import inspect
import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import stats
import stats_ui
from vibeforma import STATE_PROCESSING, STATE_RECORDING
from vibeforma.service import Service

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app" / "vibeforma" / "service.py"
SETTINGS = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"
LANGS = ("en", "ru")


def _epoch(year, month, day):
    """Noon local time: a stable day key in every time zone."""
    return time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))


def _empty():
    return {"dictations": 0, "words": 0, "chars": 0, "seconds": 0.0}


class _Log:
    def __init__(self):
        self.lines = []

    def _write(self, fmt, args):
        self.lines.append(fmt % args if args else fmt)

    def info(self, fmt, *args):
        self._write(fmt, args)

    def warning(self, fmt, *args):
        self._write(fmt, args)

    def debug(self, fmt, *args):
        self._write(fmt, args)

    def exception(self, fmt, *args):
        self._write(fmt, args)


class _Recorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class _ASR:
    def __init__(self, text):
        self.text = text

    def transcribe(self, _buf):
        return self.text


def make_service(tmp_path):
    cfg = {"locale": "en", "dictionary": [], "text_rules": False,
           "notify_text": False, "history": False}
    svc = Service(cfg, _Log())
    svc._running = True
    svc.stats = stats.Stats(str(tmp_path / "stats.json"))
    return svc


# -- storage ---------------------------------------------------------------
def test_missing_file_reads_as_empty(tmp_path):
    data = stats.Stats(str(tmp_path / "stats.json")).load()
    assert data == {"total": _empty(), "days": []}


def test_record_keeps_totals_and_the_day(tmp_path):
    store = stats.Stats(str(tmp_path / "stats.json"))
    store.record(chars=17, words=3, seconds=4.5, ts=_epoch(2026, 11, 22))

    data = store.load()
    assert data["total"] == {"dictations": 1, "words": 3, "chars": 17,
                             "seconds": 4.5}
    assert data["days"] == [{"date": stats.local_day(_epoch(2026, 11, 22)),
                             "dictations": 1, "words": 3, "chars": 17,
                             "seconds": 4.5}]


def test_days_are_bucketed_and_ordered(tmp_path):
    store = stats.Stats(str(tmp_path / "stats.json"))
    store.record(chars=5, words=1, seconds=1.0, ts=_epoch(2026, 11, 21))
    store.record(chars=7, words=2, seconds=2.0, ts=_epoch(2026, 11, 20))

    days = store.load()["days"]
    assert [row["date"] for row in days] == [
        stats.local_day(_epoch(2026, 11, 20)),
        stats.local_day(_epoch(2026, 11, 21)),
    ]
    assert days[0]["chars"] == 7
    assert days[1]["chars"] == 5


def test_second_dictation_adds_to_the_same_day(tmp_path):
    store = stats.Stats(str(tmp_path / "stats.json"))
    first = _epoch(2026, 11, 22)
    store.record(chars=10, words=2, seconds=1.25, ts=first)
    store.record(chars=30, words=4, seconds=2.5, ts=first)

    data = store.load()
    assert data["total"] == {"dictations": 2, "words": 6, "chars": 40,
                             "seconds": 3.75}
    assert len(data["days"]) == 1


def test_reset_forgets_everything(tmp_path):
    path = tmp_path / "stats.json"
    store = stats.Stats(str(path))
    store.record(chars=10, words=2, seconds=1.0)

    store.reset()

    assert not path.exists()
    assert store.load() == {"total": _empty(), "days": []}


def test_broken_json_loads_empty_and_record_recovers(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text("not json at all", encoding="utf-8")
    store = stats.Stats(str(path))

    assert store.load()["total"] == _empty()

    store.record(chars=3, words=1, seconds=0.5)
    assert store.load()["total"]["dictations"] == 1


def test_wrong_structure_is_ignored(tmp_path):
    path = tmp_path / "stats.json"
    for payload in ('[]', '{"days": 5}', '{"other": {}}'):
        path.write_text(payload, encoding="utf-8")
        assert stats.Stats(str(path)).load()["days"] == []


def test_invalid_rows_are_skipped(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"days": {
        "not-a-date": {"dictations": 9},
        "2026-11-22": {"dictations": 2, "words": 4, "chars": 9,
                       "seconds": 1.5},
        "2026-13-40": {"dictations": 7},
        "2026-11-23": [],
    }}), encoding="utf-8")

    days = stats.Stats(str(path)).load()["days"]
    assert [row["date"] for row in days] == ["2026-11-22"]
    assert days[0]["dictations"] == 2


def test_atomic_write_leaves_no_temp_file(tmp_path):
    path = tmp_path / "stats.json"
    store = stats.Stats(str(path))
    store.record(chars=3, words=1, seconds=0.5)

    assert not os.path.exists(str(path) + ".tmp")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["days"]) == 1


def test_threads_do_not_lose_records(tmp_path):
    store = stats.Stats(str(tmp_path / "stats.json"))
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            for _ in range(25):
                store.record(chars=2, words=1, seconds=0.5)
        except Exception as e:  # noqa: BLE001 - surface a worker failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    data = store.load()
    assert data["total"] == {"dictations": 200, "words": 200, "chars": 400,
                             "seconds": 100.0}
    assert len(data["days"]) == 1


def test_stats_path_sits_beside_config():
    assert stats.stats_path("C:/data") == os.path.join("C:/data",
                                                       "stats.json")


def test_stats_module_keeps_no_logger():
    source = inspect.getsource(stats)
    assert "logging" not in source
    assert ".info(" not in source


# -- display helpers -------------------------------------------------------
def test_format_number_groups_thousands():
    assert stats_ui.format_number(0) == "0"
    assert stats_ui.format_number(7) == "7"
    assert stats_ui.format_number(1234567) == "1 234 567"


def test_format_duration_reads_as_a_clock():
    assert stats_ui.format_duration(0) == "0:00"
    assert stats_ui.format_duration(65) == "1:05"
    assert stats_ui.format_duration(3661) == "1:01:01"


def test_table_rows_show_the_newest_thirty_days():
    start = date(2026, 10, 1)
    days = [{"date": (start + timedelta(days=i)).isoformat(),
             "dictations": 1, "words": 2, "chars": 3, "seconds": 4.0}
            for i in range(32)]

    rows = stats_ui.table_rows({"days": days})

    assert len(rows) == stats_ui.SHOWN_DAYS
    assert rows[0][0] == (start + timedelta(days=31)).isoformat()
    assert rows[-1][0] == (start + timedelta(days=2)).isoformat()


# -- service hook ----------------------------------------------------------
def test_finished_dictation_is_counted_once(tmp_path):
    svc = make_service(tmp_path)
    svc.asr = _ASR("hello brave world")
    svc._panel_mode = True
    svc._record_seconds = 7.5
    svc.state = STATE_PROCESSING

    svc._transcribe_worker(object(), _Recorder())

    data = svc.stats.load()
    assert data["total"]["dictations"] == 1
    assert data["total"]["words"] == 3
    assert data["total"]["chars"] == len("hello brave world")
    assert data["total"]["seconds"] == 7.5
    assert svc.live["draft"] == "hello brave world"


def test_empty_text_is_not_counted(tmp_path):
    svc = make_service(tmp_path)
    svc.asr = _ASR("")
    svc._panel_mode = True
    svc.state = STATE_PROCESSING

    svc._transcribe_worker(object(), _Recorder())

    assert svc.stats.load()["total"]["dictations"] == 0


def test_mic_test_is_not_counted(tmp_path):
    svc = make_service(tmp_path)
    svc.asr = _ASR("microphone check")
    svc._test_mode = True
    svc.state = STATE_PROCESSING

    svc._transcribe_worker(object(), _Recorder())

    assert svc.live["test_text"] == "microphone check"
    assert svc.stats.load()["total"]["dictations"] == 0


def test_cancelled_dictation_is_not_counted(tmp_path):
    svc = make_service(tmp_path)
    svc.asr = _ASR("dropped phrase")
    svc._panel_mode = True
    svc._cancel_evt.set()
    svc.state = STATE_PROCESSING

    svc._transcribe_worker(object(), _Recorder())

    assert svc.stats.load()["total"]["dictations"] == 0


def test_counting_runs_once_before_the_draft_and_insert_branches():
    source = SERVICE.read_text(encoding="utf-8")
    rules = source.index("textrules.apply(")
    count = source.index("self._count_dictation(text)")
    draft = source.index("if self._panel_mode:", count)
    assert rules < count < draft
    assert source.count("self._count_dictation(text)") == 1


def test_stop_recording_measures_the_recording_window(tmp_path):
    svc = make_service(tmp_path)
    svc.recorder = _Recorder()
    svc._record_started = time.time() - 2.0
    svc._transcribe_worker = lambda *_args: None

    svc.stop_recording("manual")

    assert svc._record_started == 0.0
    assert 1.9 <= svc._record_seconds <= 30.0


def test_cancel_clears_the_recording_window(tmp_path):
    svc = make_service(tmp_path)
    svc.recorder = _Recorder()
    svc._record_started = time.time()
    svc._record_seconds = 3.0
    svc.state = STATE_RECORDING

    svc.cancel_recording()

    assert svc._record_started == 0.0
    assert svc._record_seconds == 0.0


# -- wiring ----------------------------------------------------------------
def test_settings_window_wires_the_stats_tab():
    source = SETTINGS.read_text(encoding="utf-8")
    assert "stats_ui.build_stats_tab(" in source
    assert '("stats", "tabs.stats")' in source
    panes = source[source.index("def _create_panes"):
                   source.index("def _nav_item")]
    assert '("stats", None)' in panes


def test_locale_entries_for_the_stats_tab():
    keys = {
        "tabs.stats", "settings.sub_stats", "stats.sub", "stats.note",
        "stats.total_dictations", "stats.total_words", "stats.total_chars",
        "stats.total_seconds", "stats.col_date", "stats.col_dictations",
        "stats.col_words", "stats.col_chars", "stats.col_seconds",
        "stats.empty", "stats.reset", "stats.reset_question",
        "stats.reset_done",
    }
    for lang in LANGS:
        with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
            strings = json.load(handle)
        assert keys <= set(strings)
        assert all(str(strings[key]).strip() for key in keys)
