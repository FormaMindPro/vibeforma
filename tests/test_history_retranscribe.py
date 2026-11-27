"""History window: re-recognizing one entry with the wider pass (card-267).

The button needs exactly one selected entry whose WAV is really on disk; the
pass runs in a worker thread with a temporary ASR and the existing
``ASR.retranscribe`` wide search. Success replaces the entry's text and
confidence reading in ``history.jsonl``, redraws the row and keeps it
selected; a failed, empty or missing take leaves the entry exactly as it was.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import types

import pytest

if sys.platform != "win32":
    pytest.skip("settings_window uses Windows APIs", allow_module_level=True)

np = pytest.importorskip("numpy")
pytest.importorskip("sounddevice")

import tkinter as tk  # noqa: E402

import audio  # noqa: E402
import history  # noqa: E402
import i18n  # noqa: E402
import settings_window  # noqa: E402

CONFIG = {"locale": "en", "model": "small", "language": "en",
          "history": True, "history_audio": True, "dictionary": []}

NEW_KEYS = (
    "history.retry",
    "history.retry_hint",
    "history.retry_select",
    "history.retry_one",
    "history.retry_no_audio",
    "history.retry_running",
    "history.retry_done",
    "history.retry_no_speech",
    "history.retry_failed",
    "history.retry_gone",
    "history.retry_busy",
    "history.retry_wait",
)


def take(seconds: float = 0.1) -> np.ndarray:
    """A deterministic 16 kHz float32 take (silence is enough for the WAV)."""
    return np.zeros(int(audio.SAMPLE_RATE * seconds), dtype=np.float32)


def store_with(tmp_path, *texts) -> history.History:
    """One entry per text; each gets a WAV unless the text ends with '+'."""
    store = history.History(str(tmp_path / "history.jsonl"))
    real_time = history.time
    tick = [1000.0]

    class _SteppedClock:
        @staticmethod
        def time():
            tick[0] += 1.0
            return tick[0]

    # A coarse CI clock can stamp two adds with the same float, and
    # replace() addresses a record by its stamp - stagger them.
    history.time = _SteppedClock
    try:
        for text in texts:
            if text.endswith("+"):
                store.add(text[:-1], wav=audio.wav_bytes(take()))
            else:
                store.add(text)
    finally:
        history.time = real_time
    return store


def only_entry(store):
    records = store.load()
    assert len(records) == 1
    return records[0]


class FakeSegment:
    def __init__(self, text, probabilities):
        self.text = text
        self.words = [types.SimpleNamespace(text="word", probability=p)
                      for p in probabilities]


class FakeAsr:
    """A stand-in temporary engine: records the ask, returns fixed readings."""

    built: list[dict] = []
    calls: list[dict] = []
    result = "new text"
    probabilities: tuple = (0.9, 0.7)
    error: Exception | None = None
    last: FakeAsr | None = None

    def __init__(self, cfg, log):
        self.cfg = dict(cfg)
        self.model = None
        self.last_segments = []
        FakeAsr.built.append(dict(cfg))
        FakeAsr.last = self

    def load(self):
        self.model = object()

    def retranscribe(self, samples):
        FakeAsr.calls.append({"model": self.cfg.get("model"),
                              "samples": len(samples)})
        if FakeAsr.error is not None:
            raise FakeAsr.error
        self.last_segments = [FakeSegment(FakeAsr.result,
                                          FakeAsr.probabilities)]
        return FakeAsr.result


class FakeService:
    def __init__(self, store, cfg):
        self.history = store
        self.cfg = cfg
        self.state = "idle"
        self.live: dict = {}
        self.tray = None

    def history_records(self):
        return self.history.load()

    def tr(self, key, **kw):
        return i18n.t(self.cfg, key, **kw)


class HistoryHost(settings_window.SettingsWindow):
    """Only the History tab, built on a bare class shell."""

    def __init__(self, root, svc):
        self.win = root
        self.svc = svc
        self.cfg = svc.cfg
        self.log = logging.getLogger("test-history-retranscribe")
        self.tr = svc.tr
        self._px = lambda value: int(round(value))
        self.rows = {}
        self._search_index = []
        self._frame_key = {}
        frame = tk.Frame(root)
        self._frame_key[frame] = "history"
        frame.pack()
        self._build_history_tab(frame)


@pytest.fixture(scope="module")
def tk_root():
    # One Tk root for the module: a fresh Tk() after a destroyed root can hit
    # the "invalid command name tcl_findLibrary" interpreter re-init bug.
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for a live Tk widget test")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture(autouse=True)
def fake_asr(monkeypatch):
    module = types.ModuleType("asr")
    module.ASR = FakeAsr
    monkeypatch.setitem(sys.modules, "asr", module)
    FakeAsr.built = []
    FakeAsr.calls = []
    FakeAsr.result = "new text"
    FakeAsr.probabilities = (0.9, 0.7)
    FakeAsr.error = None
    FakeAsr.last = None
    yield module


@pytest.fixture()
def make_host(tk_root, tmp_path):
    def build(texts):
        store = store_with(tmp_path, *texts)
        host = HistoryHost(tk_root, FakeService(store, dict(CONFIG)))
        return host, store

    yield build
    for child in tk_root.winfo_children():
        child.destroy()


def wait_for_outcome(host, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while host._retry_outcome is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host._retry_outcome is not None, "the worker did not finish"


def run_pass(host):
    """Click the button and drive the poll the way the window's loop does."""
    host._retranscribe_selected()
    wait_for_outcome(host)
    host._poll_retranscribe()


def retry_state(host) -> str:
    return str(host._his_buttons["retry"].cget("state"))


def select(host, iid: str):
    host.his_tree.selection_set(iid)
    host._sync_history_actions()


def disk_lines(store):
    with open(store.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# -- the button ------------------------------------------------------------
def test_the_button_needs_one_selected_entry_with_its_wav(make_host):
    host, _store = make_host(("with sound+", "text only"))

    assert retry_state(host) == "disabled"
    assert host._retry_hint.text == host.tr("history.retry_select")

    select(host, "0")                     # the WAV entry
    assert retry_state(host) == "normal"
    assert host._retry_hint.text == host.tr("history.retry_hint")
    assert str(host._his_buttons["retry"].cget("text")) == \
        host.tr("history.retry")

    select(host, "1")                     # the text-only entry
    assert retry_state(host) == "disabled"
    assert host._retry_hint.text == host.tr("history.retry_no_audio")


def test_a_multi_selection_is_not_a_pass(make_host):
    host, _store = make_host(("one+", "two+"))

    host.his_tree.selection_set(("0", "1"))
    host._sync_history_actions()

    assert retry_state(host) == "disabled"
    assert host._retry_hint.text == host.tr("history.retry_one")


def test_a_wav_gone_after_the_row_was_drawn_disables_the_button(make_host):
    host, store = make_host(("with sound+",))
    select(host, "0")
    assert retry_state(host) == "normal"

    path = store.record_audio_path(only_entry(store))
    os.remove(path)
    host._sync_history_actions()

    assert retry_state(host) == "disabled"
    assert host._retry_hint.text == host.tr("history.retry_no_audio")


# -- the pass --------------------------------------------------------------
def test_success_replaces_the_text_and_confidence_and_redraws_the_row(
        make_host):
    host, store = make_host(("old text+",))
    record = only_entry(store)
    path = store.record_audio_path(record)
    select(host, "0")
    assert host.svc.live == {}
    # The dictionary is configured but must not rewrite the engine's text:
    # the pass replaces the entry with what the wider search returned.
    host.cfg["dictionary"] = [{"from": "new", "to": "OLD"}]

    run_pass(host)

    record = only_entry(store)
    assert record["text"] == "new text"
    assert record["chars"] == 8
    assert record["confidence"] == pytest.approx(0.8)
    assert record["audio"] and os.path.isfile(path)
    assert disk_lines(store)[0]["text"] == "new text"
    assert disk_lines(store)[0]["confidence"] == pytest.approx(0.8)

    values = host.his_tree.item("0", "values")
    assert str(values[1]) == "8"
    assert values[3] == "new text"
    assert host.his_tree.selection() == ("0",)
    assert host.his_status.cget("text") == host.tr("history.retry_done", n=8)
    assert retry_state(host) == "normal"

    assert host.svc.live == {}            # the draft is never touched
    assert FakeAsr.calls == [{"model": "small", "samples": len(take())}]
    assert FakeAsr.last is not None and FakeAsr.last.model is None


def test_the_pass_updates_only_the_selected_entry(make_host):
    host, store = make_host(("first+", "second+"))
    ts = float(store.load()[0]["ts"])
    select(host, "0")

    run_pass(host)

    records = store.load()
    assert [r["text"] for r in records] == ["new text", "second"]
    assert float(records[0]["ts"]) == ts


def test_a_failed_pass_keeps_the_entry_and_reports(make_host):
    host, store = make_host(("old text+",))
    FakeAsr.error = RuntimeError("engine failed")
    select(host, "0")

    run_pass(host)

    record = only_entry(store)
    assert record["text"] == "old text"
    assert record["chars"] == len("old text")
    assert "confidence" not in record
    assert "audio" in record
    assert host.his_status.cget("text") == host.tr("history.retry_failed")
    assert retry_state(host) == "normal"
    assert host._his_progress.winfo_manager() == ""
    assert FakeAsr.last is not None and FakeAsr.last.model is None


def test_a_record_gone_during_the_pass_is_reported(make_host, monkeypatch):
    host, store = make_host(("with sound+",))
    select(host, "0")
    monkeypatch.setattr(store, "replace", lambda *_a, **_k: False)

    run_pass(host)

    assert host.his_status.cget("text") == host.tr("history.retry_gone")
    assert only_entry(store)["text"] == "with sound"


def test_an_empty_pass_keeps_the_entry(make_host):
    host, store = make_host(("old text+",))
    FakeAsr.result = ""
    select(host, "0")

    run_pass(host)

    assert only_entry(store)["text"] == "old text"
    assert host.his_status.cget("text") == host.tr("history.retry_no_speech")


def test_a_missing_wav_at_the_click_is_refused_before_the_engine_runs(
        make_host):
    host, store = make_host(("with sound+",))
    select(host, "0")
    os.remove(store.record_audio_path(only_entry(store)))

    host._retranscribe_selected()

    assert host._retry_busy is False
    assert FakeAsr.calls == []
    assert host.his_status.cget("text") == host.tr("history.retry_no_audio")
    assert only_entry(store)["text"] == "with sound"


def test_the_pass_is_refused_while_a_take_is_in_flight(make_host):
    host, _store = make_host(("with sound+",))
    host.svc.state = "processing"
    select(host, "0")

    host._retranscribe_selected()

    assert host._retry_busy is False
    assert FakeAsr.calls == []
    assert host.his_status.cget("text") == host.tr("history.retry_wait")


def test_the_pass_runs_in_the_background_and_the_table_stays_usable(
        make_host, monkeypatch):
    host, _store = make_host(("with sound+",))
    select(host, "0")
    started = threading.Event()
    release = threading.Event()

    def slow_worker(ts, path):
        started.set()
        assert release.wait(10)
        host.svc.history.replace(ts, "slow text", confidence=0.5)
        host._retry_outcome = {"ts": ts, "chars": 9}

    monkeypatch.setattr(host, "_retranscribe_worker", slow_worker)
    t0 = time.monotonic()
    host._retranscribe_selected()
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, "the click waited on the pass"
    assert started.wait(10)
    assert host._retry_busy is True
    assert host.his_status.cget("text") == host.tr("history.retry_running")
    assert host._his_progress.winfo_manager() == "pack"
    assert retry_state(host) == "disabled"
    assert host._his_buttons["copy"].cget("state") == "normal"

    release.set()
    wait_for_outcome(host)
    host._poll_retranscribe()

    assert host._retry_busy is False
    assert host._his_progress.winfo_manager() == ""
    assert host.his_tree.selection() == ("0",)
    assert host.his_status.cget("text") == host.tr("history.retry_done", n=9)


# -- the record update -----------------------------------------------------
def test_replace_addresses_the_stamp_and_keeps_the_sound(tmp_path):
    store = store_with(tmp_path, "first+", "second")
    ts = float(store.load()[0]["ts"])
    path = store.record_audio_path(store.load()[0])

    assert store.replace(ts, "new first", confidence=0.42) is True

    records = store.load()
    assert [r["text"] for r in records] == ["new first", "second"]
    assert records[0]["chars"] == len("new first")
    assert records[0]["confidence"] == pytest.approx(0.42)
    assert records[0]["audio"] and os.path.isfile(path)
    assert disk_lines(store)[0]["text"] == "new first"
    assert "confidence" not in records[1]


def test_replace_refuses_an_unknown_stamp_and_empty_text(tmp_path):
    store = store_with(tmp_path, "first+")
    ts = float(store.load()[0]["ts"])

    assert store.replace(ts + 1.0, "new") is False
    assert store.replace(ts, "   ") is False
    assert store.replace("not a stamp", "new") is False
    assert store.load()[0]["text"] == "first"


def test_replace_never_keeps_a_stale_confidence(tmp_path):
    store = store_with(tmp_path, "first+")
    ts = float(store.load()[0]["ts"])
    store.replace(ts, "new", confidence=0.9)

    store.replace(ts, "newer", confidence=None)

    assert store.load()[0]["text"] == "newer"
    assert "confidence" not in store.load()[0]


def test_replace_clamps_the_reading_to_a_probability(tmp_path):
    store = store_with(tmp_path, "first+")
    ts = float(store.load()[0]["ts"])

    store.replace(ts, "new", confidence=1.7)

    assert store.load()[0]["confidence"] == 1.0


def test_confidence_is_the_mean_word_probability():
    segments = [FakeSegment("a b", [0.9, 0.7]),
                FakeSegment("c", [1.0])]
    assert settings_window.retranscription_confidence(segments) == \
        pytest.approx(0.8667)

    assert settings_window.retranscription_confidence([]) is None
    assert settings_window.retranscription_confidence(
        [FakeSegment("a", [])]) is None
    assert settings_window.retranscription_confidence(
        [FakeSegment("a", [None])]) is None


# -- the catalogs ----------------------------------------------------------
def test_the_new_keys_are_in_both_catalogs():
    root = settings_window.__file__
    import os
    folder = os.path.join(os.path.dirname(root), "locales")
    catalogs = {}
    for lang in ("en", "ru"):
        with open(os.path.join(folder, f"{lang}.json"), encoding="utf-8") as f:
            catalogs[lang] = json.load(f)
    for key in NEW_KEYS:
        for lang in ("en", "ru"):
            assert str(catalogs[lang].get(key, "")).strip(), (lang, key)
    assert "{n}" in catalogs["en"]["history.retry_done"]
    assert "{n}" in catalogs["ru"]["history.retry_done"]
