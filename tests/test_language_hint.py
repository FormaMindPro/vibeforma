"""Language hint: the pinned language stays, the HUD names the mismatch.

Card-249 closes B9: the engine's language reading used to be thrown away, so
a person dictating English with Russian pinned heard nothing about the
mismatch. The final dictation pass now keeps a detection reading, the service
publishes it, and the HUD shows "looks like X (..%), Y is pinned (..%)" while
recognition keeps using Y. The hint needs both readings at once: a pinned
language below the floor and another language above the lead.
"""
import json
import logging
import sys
import types
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first
import hud  # noqa: E402
from vibeforma.service import Service  # noqa: E402

SAMPLE_RATE = 16000
LOCALES = Path(__file__).resolve().parents[1] / "app" / "locales"


def _locale(key, lang="en", **values):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        template = json.load(handle)[key]
    return template.format(**values)


def _tr(key, **kw):
    return _locale(key, **kw)


class RecordingLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def debug(self, fmt, *args):
        self._add("debug", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)


class _Info:
    """A pinned call's own info: the language it was told, certainty 1."""

    language = "ru"
    language_probability = 1.0


class _EngineSegment:
    def __init__(self, text):
        self.seek = 0
        self.start = 0.0
        self.end = 1.0
        self.text = text
        self.words = None
        self.avg_logprob = -0.2
        self.compression_ratio = 1.1
        self.no_speech_prob = 0.02
        self.temperature = 0.0


class _Model:
    """A model whose detection reading the test sets; no real weights."""

    is_multilingual = True

    def __init__(self, probs):
        self.probs = list(probs or [])
        self.fail = False
        self.calls = []

    def transcribe(self, _audio, **kwargs):
        self.calls.append(kwargs)
        return [_EngineSegment(" hello")], _Info()

    def detect_language(self, _audio):
        if self.fail:
            raise RuntimeError("no detection")
        detected, probability = max(self.probs, key=lambda row: row[1])
        return detected, probability, list(self.probs)


def _engine(probs, pinned="ru"):
    engine = asr.ASR({"language": pinned, "hotwords": "", "dictionary": []},
                     logging.getLogger("test_language_hint"))
    model = _Model(probs)
    engine.model = model
    return engine, model


def _audio():
    return np.zeros(SAMPLE_RATE, dtype=np.float32)


# -- the condition itself, apart from the engine ---------------------------

def test_a_hint_needs_a_low_pinned_language_and_a_leading_other_one():
    hint = asr.language_hint(
        [("en", 0.94), ("ru", 0.03), ("uk", 0.02)], "ru")

    assert hint == {"pinned": "ru", "pinned_prob": 0.03,
                    "other": "en", "other_prob": 0.94}


def test_a_confident_pinned_language_never_hints():
    assert asr.language_hint([("ru", 0.97), ("en", 0.02)], "ru") is None


def test_a_spread_out_reading_never_hints():
    # The pinned language is unsure, but no other language is high either:
    # a hint here would name a language the engine itself does not lead with.
    assert asr.language_hint(
        [("ru", 0.30), ("en", 0.30), ("de", 0.20)], "ru") is None


def test_a_pinned_language_missing_from_the_reading_never_hints():
    # Without the pinned language's own probability there is nothing to call
    # low, so the hint is not invented from an absent reading.
    assert asr.language_hint([("en", 0.90), ("de", 0.05)], "ru") is None


# -- the final dictation pass keeps the reading ----------------------------

def test_a_final_take_keeps_the_detection_reading_and_the_pinned_language():
    engine, model = _engine([("en", 0.93), ("ru", 0.04), ("uk", 0.02)])

    text = engine.transcribe(_audio())

    assert text == "hello"
    # Recognition still used the pinned language: the hint never switches it.
    assert model.calls[-1]["language"] == "ru"
    hint = engine.last_language_hint
    assert hint is not None
    assert hint["pinned"] == "ru"
    assert hint["other"] == "en"
    assert hint["pinned_prob"] == 0.04
    assert hint["other_prob"] == 0.93
    assert hint["ts"] > 0


def test_a_confident_russian_take_leaves_no_hint():
    engine, _model = _engine([("ru", 0.96), ("en", 0.02)])

    engine.transcribe(_audio())

    assert engine.last_language_hint is None


def test_a_partial_never_touches_the_hint():
    engine, _model = _engine([("en", 0.93), ("ru", 0.04)])
    engine.transcribe(_audio())
    hint = engine.last_language_hint

    engine.transcribe(_audio(), verbose=False, partial=True)

    assert engine.last_language_hint is hint


def test_a_failed_detection_never_costs_the_take():
    engine, model = _engine([("en", 0.93), ("ru", 0.04)])
    model.fail = True

    text = engine.transcribe(_audio())

    assert text == "hello"
    assert engine.last_language_hint is None


def test_an_english_only_model_never_hints():
    engine, model = _engine([("ru", 0.93), ("en", 0.04)])
    model.model = types.SimpleNamespace(is_multilingual=False)

    engine.transcribe(_audio())

    assert engine.last_language_hint is None


# -- the service publishes it, the HUD renders it --------------------------

class FakeRecorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class FakeASR:
    def __init__(self, text, hint):
        self.text = text
        self.last_segments = []
        self.last_language_hint = hint

    def transcribe(self, _buf):
        return self.text


def _make_service():
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


def _fake_insert(monkeypatch):
    calls = []
    fake = types.ModuleType("insert")
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.get_foreground_hwnd = lambda: 0
    fake.insert_text = lambda text, **kwargs: (
        calls.append(text) or "inserted")
    monkeypatch.setitem(sys.modules, "insert", fake)
    return calls


def test_the_service_publishes_the_take_s_language_reading(monkeypatch):
    hint = {"pinned": "ru", "pinned_prob": 0.04,
            "other": "en", "other_prob": 0.93, "ts": 1000.0}
    svc = _make_service()
    svc.asr = FakeASR("hello there", hint)
    svc.state = "processing"
    _fake_insert(monkeypatch)

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.live["language_hint"] == hint
    assert svc.live["draft"] == ""
    assert svc.live["state"] == "idle"


def test_the_service_clears_the_previous_hint_on_a_new_take(monkeypatch):
    svc = _make_service()
    svc.asr = FakeASR("hello there", None)
    svc.state = "processing"
    svc.live["language_hint"] = {"pinned": "ru", "other": "en", "ts": 1.0}
    _fake_insert(monkeypatch)

    svc._transcribe_worker(object(), FakeRecorder())

    assert svc.live["language_hint"] is None


def test_the_hud_names_both_languages_with_probabilities():
    now = 1000.0
    hint = {"pinned": "ru", "pinned_prob": 0.04,
            "other": "en", "other_prob": 0.93, "ts": now - 1.0}

    text = hud.language_hint_text(hint, _tr, now=now)

    assert text == _locale("hud.language_hint", other="English",
                           other_pct=93, pinned="\u0420\u0443\u0441\u0441\u043a"
                           "\u0438\u0439", pinned_pct=4)
    assert "English" in text
    assert "(93%)" in text
    assert "(4%)" in text


def test_the_russian_catalog_says_what_the_hud_shows():
    rendered = _locale("hud.language_hint", lang="ru", other="English",
                       other_pct=93, pinned="\u0420\u0443\u0441\u0441\u043a"
                       "\u0438\u0439", pinned_pct=4)

    assert rendered == ("\u043f\u043e\u0445\u043e\u0436\u0435 \u043d\u0430 "
                        "English (93%), \u0432\u044b\u0431\u0440\u0430\u043d "
                        "\u0420\u0443\u0441\u0441\u043a\u0438\u0439 (4%)")


def test_a_stale_hint_is_not_shown():
    now = 1000.0
    hint = {"pinned": "ru", "pinned_prob": 0.04,
            "other": "en", "other_prob": 0.93,
            "ts": now - hud.LANGUAGE_HINT_TTL_S - 1.0}

    assert hud.language_hint_text(hint, _tr, now=now) == ""


def test_no_hint_is_no_text():
    assert hud.language_hint_text(None, _tr) == ""
