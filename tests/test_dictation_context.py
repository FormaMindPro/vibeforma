"""Dictation context (F11): the previous phrase and the selection as hotwords."""
import sys
import types

from vibeforma import STATE_IDLE, STATE_RECORDING
from vibeforma.service import Service, _context_tail


class _Log:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class _Recorder:
    def had_speech(self):
        return True


class _Asr:
    """A fake engine that records the hotwords its config carries per call."""

    def __init__(self, texts, cfg):
        self.texts = list(texts)
        self.cfg = cfg
        self.shared = cfg
        self.hotwords = []
        self.own_dict = []
        self.last_segments = []

    def transcribe(self, _samples, verbose=True, *, partial=False):
        self.hotwords.append(self.cfg.get("hotwords"))
        self.own_dict.append(self.cfg is self.shared)
        return self.texts.pop(0) if self.texts else ""


def _service(**overrides):
    cfg = {
        "locale": "en",
        "dictionary": [],
        "snippets": [],
        "history": False,
        "hud_privacy": False,
        "notify_text": False,
        "confirm_focus_change": False,
    }
    cfg.update(overrides)
    svc = Service(cfg, _Log())
    svc.stats = None
    return svc


def _fake_insert(monkeypatch, selected="", reader=None):
    """A stand-in for the insert module; the selection comes from the test."""
    module = types.ModuleType("insert")
    module.RESULT_PASTED = "pasted"
    module.RESULT_COPIED = "copied"
    module.RESULT_CANCELLED = "cancelled"
    module.get_foreground_hwnd = lambda: 0
    module.insert_text = lambda text, **kwargs: "pasted"

    if reader is not None:
        module.read_selected_text = reader
    else:
        def _selected(log=None):
            return selected
        module.read_selected_text = _selected
    monkeypatch.setitem(sys.modules, "insert", module)
    return module


def _take(svc, asr):
    svc._capture_dictation_context()
    svc._transcribe_worker(object(), _Recorder())
    return asr


# -- the option is on ------------------------------------------------------

def test_the_previous_phrase_reaches_the_next_take(monkeypatch):
    _fake_insert(monkeypatch, selected="")
    svc = _service(dictation_context=True)
    asr = _Asr(["\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432",
                "\u0441\u043e\u0437\u0432\u043e\u043d"], svc.cfg)
    svc.asr = asr

    _take(svc, asr)
    assert svc._last_phrase == "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"
    assert asr.hotwords[0] in (None, "")

    _take(svc, asr)

    assert asr.hotwords[1] == "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"


def test_the_selected_text_of_the_window_reaches_the_take(monkeypatch):
    _fake_insert(monkeypatch, selected="VibeForma")
    svc = _service(dictation_context=True)
    asr = _Asr(["\u043f\u0440\u0438\u0432\u0435\u0442"], svc.cfg)
    svc.asr = asr

    svc._capture_dictation_context()
    assert svc._context_hotwords == "VibeForma"

    svc._transcribe_worker(object(), _Recorder())

    assert "VibeForma" in asr.hotwords[0]


def test_the_previous_phrase_and_the_selection_are_joined(monkeypatch):
    _fake_insert(monkeypatch, selected="VibeForma")
    svc = _service(dictation_context=True)
    svc._last_phrase = "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"

    svc._capture_dictation_context()

    assert svc._context_hotwords == "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432, VibeForma"


def test_the_users_hotwords_stay_in_front_and_are_never_rewritten(monkeypatch):
    _fake_insert(monkeypatch, selected="")
    svc = _service(dictation_context=True, hotwords="Forma")
    asr = _Asr(["\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432",
                "\u0441\u043e\u0437\u0432\u043e\u043d"], svc.cfg)
    svc.asr = asr

    _take(svc, asr)
    _take(svc, asr)

    assert asr.hotwords[1] == "Forma, \u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"
    # The engine got a copy for the call; the shared config is untouched.
    assert asr.own_dict[1] is False
    assert asr.cfg is svc.cfg
    assert svc.cfg["hotwords"] == "Forma"


def test_the_panel_reads_no_selection(monkeypatch):
    _fake_insert(monkeypatch, selected="VibeForma")
    svc = _service(dictation_context=True, hotwords="")
    svc._panel_mode = True

    svc._capture_dictation_context()

    assert svc._context_hotwords == ""


def test_a_cancelled_take_keeps_no_context(monkeypatch):
    _fake_insert(monkeypatch, selected="VibeForma")
    svc = _service(dictation_context=True)
    svc._last_phrase = "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"
    svc._capture_dictation_context()
    svc.state = STATE_RECORDING

    svc.cancel_recording()

    assert svc._context_hotwords == ""
    assert svc.state == STATE_IDLE


# -- the option is off (the default) --------------------------------------

def test_off_means_the_context_is_never_read(monkeypatch):
    calls = []

    def _must_not_run(log=None):
        calls.append(True)
        raise AssertionError("the selection was read with the option off")

    _fake_insert(monkeypatch, reader=_must_not_run)
    svc = _service()
    asr = _Asr(["\u0441\u043e\u0437\u0432\u043e\u043d"], svc.cfg)
    svc.asr = asr
    svc._last_phrase = "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"

    _take(svc, asr)

    assert calls == []
    assert svc._context_hotwords == ""
    assert asr.hotwords[0] in (None, "")


def test_switching_the_option_off_drops_the_kept_context(monkeypatch):
    _fake_insert(monkeypatch, selected="VibeForma")
    svc = _service(dictation_context=True)
    svc._capture_dictation_context()
    assert svc._context_hotwords == "VibeForma"

    svc.cfg["dictation_context"] = False
    svc._capture_dictation_context()

    assert svc._context_hotwords == ""


# -- the bounded tail ------------------------------------------------------

def test_the_context_tail_keeps_the_end_of_the_phrase():
    text = "one two three four five six seven eight nine ten"
    tail = _context_tail(text, limit=20)

    assert len(tail) <= 20
    assert text.endswith(tail)
    assert not tail.startswith(" ")


def test_the_context_tail_puts_a_multiline_selection_on_one_line():
    assert _context_tail("first line\n\nsecond\tline") == "first line second line"
