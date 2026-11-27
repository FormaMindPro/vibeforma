"""Draft retry (card-247): the same take read again wider, with a rollback.

A doubtful draft offers ``Recognize more precisely``: the take's WAV is read
again by the same configured model at beam 10 and temperature 0, and the
result replaces the draft while the previous text stays available behind
``Restore previous``. The pass never inserts, never runs the hook and never
re-applies the dictionary or the snippets: only the engine's text replaces
the draft.
"""
import json
import logging
import sys
import types
from pathlib import Path

import pytest

try:
    import sounddevice  # noqa: F401
except Exception:  # noqa: BLE001 - init fails on a host with no audio server
    pytest.skip("sounddevice cannot initialize here", allow_module_level=True)

import asr
import audio
import hud
import numpy as np
import transcript
from transcript import Segment, Word
from vibeforma import service as service_mod
from vibeforma.service import Service

SAMPLE_RATE = 16000


class RecordingLog:
    def __init__(self):
        self.lines = []

    def _add(self, fmt, *args):
        self.lines.append(fmt % args if args else str(fmt))

    def info(self, fmt, *args):
        self._add(fmt, *args)

    def warning(self, fmt, *args):
        self._add(fmt, *args)

    def exception(self, fmt, *args):
        self._add(fmt, *args)

    def debug(self, fmt, *args):
        self._add(fmt, *args)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Keep the stats/history writes of these dictations out of the real app."""
    monkeypatch.setattr(service_mod, "DATA_DIR", str(tmp_path))


# -- engine: the wider pass -------------------------------------------------

class _Info:
    language = "en"


class _EngineWord:
    def __init__(self, word, probability):
        self.word = word
        self.probability = probability
        self.start = 0.0
        self.end = 0.4


class _EngineSegment:
    def __init__(self, text, *, words=None, avg_logprob=-0.2,
                 compression_ratio=1.1, no_speech_prob=0.05,
                 temperature=0.0):
        self.seek = 0
        self.start = 0.0
        self.end = 1.0
        self.text = text
        self.words = words
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio
        self.no_speech_prob = no_speech_prob
        self.temperature = temperature


class _Model:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, _audio, **kwargs):
        self.calls.append(kwargs)
        return list(self.segments), _Info()


def _engine(segments, cfg=None):
    engine = asr.ASR(cfg or {"language": "en", "hotwords": "",
                             "dictionary": []},
                     logging.getLogger("test_retranscribe"))
    model = _Model(segments)
    engine.model = model
    return engine, model


def test_the_wider_pass_uses_beam_ten_at_temperature_zero():
    engine, model = _engine([
        _EngineSegment(" clearer", words=[_EngineWord(" clearer", 0.97)]),
    ])

    text = engine.retranscribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    assert text == "clearer"
    call = model.calls[-1]
    assert call["beam_size"] == asr.RETRANSCRIBE_BEAM_SIZE == 10
    assert call["temperature"] == 0.0
    assert call["word_timestamps"] is True
    assert engine.model is model
    assert engine.last_segments[0].text == " clearer"


def test_the_wider_pass_updates_the_readings_for_the_new_text():
    engine, _model = _engine([
        _EngineSegment(" clearer", words=[_EngineWord(" clearer", 0.97)],
                       avg_logprob=-0.12, no_speech_prob=0.02,
                       temperature=0.0),
    ])

    engine.retranscribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    row = engine.last_segments[0]
    assert row.avg_logprob == -0.12
    assert row.temperature == 0.0
    assert transcript.needs_review(engine.last_segments) is False
    assert transcript.uncertain_words(engine.last_segments) == []


def test_the_engine_post_filter_runs_before_the_text_is_returned():
    engine, _model = _engine([
        _EngineSegment(" real speech", words=[_EngineWord(" real", 0.9)]),
        _EngineSegment(" thanks for watching", no_speech_prob=0.92),
    ])

    text = engine.retranscribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    assert text == "real speech"
    assert [row.text for row in engine.last_segments] == [" real speech"]


def test_the_wider_pass_builds_no_second_model(monkeypatch):
    engine, _model = _engine([_EngineSegment(" hello")])

    def _boom(*_args, **_kwargs):
        raise AssertionError("a retry must never load or download a model")

    monkeypatch.setattr(asr, "_make_model", _boom)

    assert engine.retranscribe(np.zeros(SAMPLE_RATE, dtype=np.float32)) == "hello"


# -- service fixtures -------------------------------------------------------

class FakeRecorder:
    def had_speech(self):
        return True


class FakeASR:
    """A stand-in engine: fixed first-take text and readings, no audio."""

    def __init__(self, text, segments):
        self.text = text
        self.last_segments = list(segments)
        self.last_language_hint = None

    def transcribe(self, _buf):
        return self.text


class RetryASR:
    """An engine whose wider pass records its audio and returns new rows."""

    def __init__(self, first, first_rows, retry, retry_rows):
        self.first = first
        self.first_rows = list(first_rows)
        self.retry = retry
        self.retry_rows = list(retry_rows)
        self.last_segments = list(first_rows)
        self.last_language_hint = None
        self.calls = []

    def transcribe(self, _buf):
        self.last_segments = list(self.first_rows)
        return self.first

    def retranscribe(self, samples):
        self.calls.append(samples)
        self.last_segments = list(self.retry_rows)
        return self.retry


def doubtful_rows(text="murky phrase"):
    return [Segment(0.0, 1.0, " " + text,
                    words=[Word(0.0, 0.4, " murky", 0.22),
                           Word(0.4, 1.0, " phrase", 0.96)],
                    avg_logprob=-1.3, compression_ratio=1.2,
                    no_speech_prob=0.68, temperature=0.2)]


def confident_rows(text="hello there"):
    return [Segment(0.0, 1.0, " " + text,
                    words=[Word(0.0, 0.4, " hello", 0.98),
                           Word(0.4, 1.0, " there", 0.97)],
                    avg_logprob=-0.15, compression_ratio=1.1,
                    no_speech_prob=0.02, temperature=0.0)]


def make_service(**overrides):
    cfg = {
        "locale": "en",
        "dictionary": [],
        "snippets": [],
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
        "hook_command": "",
        "confirm_focus_change": True,
        "restore_clipboard": False,
        "paste_method": "shift_insert",
        "clipboard_exclude_history": True,
        "log_window_titles": False,
    }
    cfg.update(overrides)
    svc = Service(cfg, RecordingLog())
    svc._running = True
    svc.state = "idle"
    return svc


def fake_insert(monkeypatch, foreground=None):
    calls = []
    fake = types.ModuleType("insert")
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.RESULT_PASTED = "pasted"
    fake.get_foreground_hwnd = lambda: foreground
    fake.insert_text = lambda text, **_kwargs: (
        calls.append(text) or "inserted")
    monkeypatch.setitem(sys.modules, "insert", fake)
    return calls


def wav_bytes(samples=1600):
    return audio.wav_bytes(np.full(samples, 0.1, dtype=np.float32))


def prepare_draft(svc, text="murky phrase", uncertain=("murky",),
                  wav=None, offer=True):
    svc._show_draft(text, wav=wav if wav is not None else wav_bytes(),
                    run_hook=False, uncertain=list(uncertain), offer=offer)


# -- offer: doubtful takes only --------------------------------------------

def test_a_doubtful_panel_take_offers_the_wider_pass_without_history():
    svc = make_service()
    svc.asr = FakeASR("murky phrase", doubtful_rows())
    svc._panel_mode = True
    svc.state = "processing"

    svc._transcribe_worker(np.full(1600, 0.1, dtype=np.float32),
                           FakeRecorder())

    assert svc.live["draft"] == "murky phrase"
    assert svc.live["draft_offer"] is True
    assert svc.live["draft_audio"] is True
    assert svc._draft_wav and svc._draft_wav[:4] == b"RIFF"
    assert svc.cfg["history"] is False


def test_temperature_above_zero_offers_and_a_confident_take_does_not(
        monkeypatch):
    warm = make_service()
    warm.asr = FakeASR("murky phrase", doubtful_rows())
    warm._panel_mode = True
    warm.state = "processing"
    warm._transcribe_worker(np.full(1600, 0.1, dtype=np.float32),
                            FakeRecorder())
    assert warm.live["draft_offer"] is True

    calm = make_service()
    calm.asr = FakeASR("hello there", confident_rows())
    calm._panel_mode = True
    calm.state = "processing"
    calm._transcribe_worker(np.full(1600, 0.1, dtype=np.float32),
                            FakeRecorder())
    assert calm.live["draft"] == "hello there"
    assert calm.live["draft_offer"] is False
    assert calm.live["draft_audio"] is False

    insert = make_service()
    insert.asr = FakeASR("hello there", confident_rows())
    insert.state = "processing"
    calls = fake_insert(monkeypatch, foreground=0)
    insert._transcribe_worker(np.full(1600, 0.1, dtype=np.float32),
                              FakeRecorder())
    assert calls == ["hello there"]          # a confident phrase is pasted
    assert insert.live["draft_offer"] is False


def test_a_doubtful_take_shown_for_a_focus_change_still_offers(monkeypatch):
    svc = make_service()
    svc.asr = FakeASR("murky phrase", doubtful_rows())
    svc.state = "processing"
    svc._target_hwnd = 7
    fake_insert(monkeypatch, foreground=8)

    svc._transcribe_worker(np.full(1600, 0.1, dtype=np.float32),
                           FakeRecorder())

    assert svc.live["draft"] == "murky phrase"
    assert svc.live["draft_offer"] is True
    assert svc.live["draft_audio"] is True


def test_a_confident_focus_change_draft_keeps_the_offer_off(monkeypatch):
    svc = make_service()
    svc.asr = FakeASR("hello there", confident_rows())
    svc.state = "processing"
    svc._target_hwnd = 7
    fake_insert(monkeypatch, foreground=8)

    svc._transcribe_worker(np.full(1600, 0.1, dtype=np.float32),
                           FakeRecorder())

    assert svc.live["draft"] == "hello there"
    assert svc.live["draft_offer"] is False


def test_a_draft_from_another_source_has_no_offer():
    svc = make_service()
    svc._show_draft("history text", run_hook=False)

    assert svc.live["draft_offer"] is False
    assert svc.live["draft_audio"] is False
    assert svc.live["draft_rollback"] is False


# -- the wider pass and the rollback --------------------------------------

def test_the_pass_replaces_the_draft_and_keeps_the_take():
    svc = make_service()
    retry_rows = confident_rows("clearer take")
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer take",
                       retry_rows)
    held = wav_bytes()
    prepare_draft(svc, wav=held)
    rev = svc.live["draft_rev"]

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "clearer take"
    assert svc.live["draft_uncertain"] == []
    assert svc.live["draft_rollback"] is True
    assert svc.live["draft_offer"] is False     # the new text is confident
    assert svc.live["draft_audio"] is True
    assert svc._draft_wav is held
    assert svc.live["draft_rev"] > rev
    assert svc.live["last"].startswith("Recognized more precisely")
    assert svc._retry_busy is False
    assert len(svc.asr.calls) == 1
    assert len(svc.asr.calls[0]) == 1600


def test_the_pass_reports_its_own_length_and_keeps_the_offer_when_doubtful():
    svc = make_service()
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "murky phrase v2",
                       doubtful_rows("murky phrase v2"))
    prepare_draft(svc)

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "murky phrase v2"
    assert svc.live["draft_offer"] is True
    assert svc.live["draft_rollback"] is True
    assert f"{len('murky phrase v2')} chars" in svc.live["last"]


def test_the_pass_never_inserts_runs_the_hook_or_reapplies_rules(
        monkeypatch):
    svc = make_service(
        dictionary=[{"from": "clearer", "to": "REWRITTEN"}],
        snippets=[{"trigger": "clearer", "text": "SNIPPET"}],
        hook_command="echo hook")
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer take",
                       confident_rows("clearer take"))
    prepare_draft(svc)
    hooked = []
    remembered = []
    svc._run_hook = lambda text: hooked.append(text)
    svc._remember = lambda *args, **kwargs: remembered.append(args)
    inserts = fake_insert(monkeypatch, foreground=0)

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "clearer take"
    assert hooked == []
    assert remembered == []
    assert inserts == []


def test_rollback_restores_the_exact_text_and_marks():
    svc = make_service()
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer take",
                       confident_rows("clearer take"))
    text = "  murky   phrase\nsecond line "
    marks = ["murky", "phrase"]
    held = wav_bytes()
    prepare_draft(svc, text=text, uncertain=marks, wav=held)
    svc._retranscribe_draft_worker()
    assert svc.live["draft"] == "clearer take"

    restored = svc.rollback_draft()

    assert restored == text
    assert svc.live["draft"] == text
    assert svc.live["draft_uncertain"] == marks
    assert svc.live["draft_rollback"] is False
    assert svc.live["draft_offer"] is True
    assert svc.live["draft_audio"] is True
    assert svc._draft_wav is held
    assert svc.live["last"] == "Previous text restored"


def test_a_second_pass_can_be_offered_after_a_rollback():
    svc = make_service()
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer take",
                       confident_rows("clearer take"))
    prepare_draft(svc)
    svc._retranscribe_draft_worker()
    svc.rollback_draft()

    # The restored take is doubtful again and its audio is still held.
    assert svc.live["draft_offer"] is True
    assert svc.live["draft_audio"] is True
    assert svc.retranscribe_draft() == ""
    assert svc._retry_busy is True
    svc._retry_busy = False


# -- failure: the draft is never lost -------------------------------------

def test_a_failed_pass_leaves_the_draft_and_the_offer(monkeypatch):
    svc = make_service()
    held = wav_bytes()
    prepare_draft(svc, text="murky phrase", uncertain=["murky"], wav=held)
    rev = svc.live["draft_rev"]

    class BrokenASR(RetryASR):
        def retranscribe(self, samples):
            raise RuntimeError("engine exploded")

    svc.asr = BrokenASR("murky phrase", doubtful_rows(), "", [])

    svc._retranscribe_draft_worker()   # never raises: a notice is the result

    assert svc.live["draft"] == "murky phrase"
    assert svc.live["draft_uncertain"] == ["murky"]
    assert svc.live["draft_offer"] is True
    assert svc.live["draft_rollback"] is False
    assert svc._draft_wav is held
    assert svc.live["last"] == "Re-recognition failed — the draft is unchanged"
    assert svc.live["draft_rev"] > rev
    assert svc._retry_busy is False


def test_an_empty_pass_leaves_the_draft():
    svc = make_service()
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "", [])
    prepare_draft(svc)

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "murky phrase"
    assert svc.live["draft_offer"] is True
    assert svc.live["last"] == ("Nothing clearer heard — the draft is "
                                "unchanged")


def test_empty_audio_is_a_notice_not_a_replacement():
    svc = make_service()
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer", [])
    prepare_draft(svc)
    svc._draft_wav = audio.wav_bytes(np.zeros(0, dtype=np.float32))

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "murky phrase"
    assert svc.live["draft_offer"] is True
    assert svc.live["last"] == ("Nothing clearer heard — the draft is "
                                "unchanged")
    assert svc.asr.calls == []


def test_a_broken_wav_is_a_localized_notice():
    svc = make_service()
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer", [])
    prepare_draft(svc)
    svc._draft_wav = b"not a wav"

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "murky phrase"
    assert svc.live["last"] == "Re-recognition failed — the draft is unchanged"


def test_a_stale_pass_never_replaces_another_take():
    svc = make_service()

    class ReplacingASR(RetryASR):
        def retranscribe(self, samples):
            svc._show_draft("another take", run_hook=False)
            return "clearer take"

    svc.asr = ReplacingASR("murky phrase", doubtful_rows(), "clearer take",
                           confident_rows("clearer take"))
    prepare_draft(svc)

    svc._retranscribe_draft_worker()

    assert svc.live["draft"] == "another take"
    assert svc.live["draft_rollback"] is False


# -- guards ----------------------------------------------------------------

@pytest.mark.parametrize("state", ["loading", "recording", "processing",
                                   "updating"])
def test_the_retry_waits_for_a_finished_cycle(state):
    svc = make_service()
    prepare_draft(svc)
    svc.state = state

    assert svc.retranscribe_draft() == ""

    assert svc._retry_busy is False
    assert svc.live["last"] == "Wait for the current cycle to finish"


def test_a_double_click_starts_one_pass(monkeypatch):
    svc = make_service()
    prepare_draft(svc)
    svc.asr = RetryASR("murky phrase", doubtful_rows(), "clearer", [])

    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            started.append(self)

        def start(self):
            pass

    monkeypatch.setattr(service_mod.threading, "Thread", FakeThread)

    assert svc.retranscribe_draft() == ""
    assert len(started) == 1
    assert svc._retry_busy is True

    assert svc.retranscribe_draft() == ""
    assert len(started) == 1


def test_a_draft_without_audio_is_not_retried():
    svc = make_service()
    prepare_draft(svc, wav=None)
    svc._draft_wav = None
    svc.live["draft_audio"] = False

    assert svc.retranscribe_draft() == ""

    assert svc._retry_busy is False
    assert svc.live["last"] == "No audio is kept for this draft"


# -- offers never cross takes ----------------------------------------------

def test_forget_clears_both_offers():
    svc = make_service()
    prepare_draft(svc)
    svc._draft_prev_text = "older"
    svc._retry_busy = False

    svc._forget_draft()

    assert svc.live["draft_offer"] is False
    assert svc.live["draft_rollback"] is False
    assert svc.live["draft_audio"] is False
    assert svc.live["draft"] == ""
    assert svc._draft_wav is None


def test_cancel_clears_both_offers():
    svc = make_service()
    prepare_draft(svc)

    svc.cancel_draft()

    assert svc.live["draft_offer"] is False
    assert svc.live["draft_rollback"] is False
    assert svc.live["draft_audio"] is False


def test_the_history_flip_clears_the_offers_and_restores_audio_only():
    svc = make_service(history=True)
    svc.history_records = lambda: [{"text": "old"}]
    prepare_draft(svc)

    svc.browse_history(-1)
    assert svc.live["draft"] == "old"
    assert svc.live["draft_offer"] is False
    assert svc.live["draft_rollback"] is False
    assert svc.live["draft_audio"] is False

    svc.browse_history(1)
    assert svc.live["draft"] == "murky phrase"
    assert svc.live["draft_audio"] is True
    assert svc.live["draft_offer"] is False
    assert svc.live["draft_rollback"] is False


def test_insert_clears_both_offers(monkeypatch):
    svc = make_service(history=False)
    prepare_draft(svc)
    fake = types.ModuleType("insert")
    fake.copy_text = lambda _text: None
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc._insert_draft_worker("murky phrase", None)

    assert svc.live["draft_offer"] is False
    assert svc.live["draft_rollback"] is False
    assert svc.live["draft_audio"] is False


# -- the HUD row -----------------------------------------------------------

class FakeText:
    def __init__(self, value=""):
        self.value = value
        self.state = "normal"
        self.height = hud.BASE_TEXT_LINES

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

    def tag_remove(self, *_args):
        pass

    def tag_add(self, *_args):
        pass

    def focus_set(self):
        pass

    def mark_set(self, *_args):
        pass

    def see(self, *_args):
        pass


class FakeWidget:
    def __init__(self):
        self.packed = False
        self.state = "normal"
        self.text = ""

    def pack(self, **_kw):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def configure(self, **kw):
        if "state" in kw:
            self.state = kw["state"]
        if "text" in kw:
            self.text = kw["text"]

    def cget(self, key):
        return self.text if key == "text" else self.state


class FakeOfferSvc:
    def __init__(self):
        self.retried = 0
        self.rolled = 0

    def retranscribe_draft(self):
        self.retried += 1
        return ""

    def rollback_draft(self):
        self.rolled += 1
        return ""


def make_offer_hud(active=True):
    h = hud.Hud.__new__(hud.Hud)
    h.cfg = {}
    h.log = RecordingLog()
    h.state = {}
    h.text = FakeText("murky phrase")
    h.svc = FakeOfferSvc()
    h._draft_active = active
    h._draft_edit = False
    h._draft_text = "murky phrase"
    h._draft_rev = 0
    h._last_foreign_hwnd = None
    h._want = hud.BASE_TEXT_LINES
    h._actions_visible = True
    h._extra_h = hud.ACTION_ROW_H
    h._offer_h = 0
    h._offers_visible = False
    h._retry_visible = False
    h._rollback_visible = False
    h.actions = FakeWidget()
    h.insert_btn = FakeWidget()
    h.offers = FakeWidget()
    h.retry_btn = FakeWidget()
    h.rollback_btn = FakeWidget()
    h.edit_btn = FakeWidget()
    h.copy_btn = FakeWidget()
    h.dict_btn = FakeWidget()
    h.cancel_btn = FakeWidget()
    h._px = lambda n: n
    h._apply_height = lambda: None
    h._show_actions = lambda: None
    h._hide_actions = lambda: None
    h._autogrow = lambda text, draft=False: None
    h._set_edit_label = lambda editing: None
    h._mark_uncertain = lambda text, words: None
    h._read_draft = lambda: h.text.value
    h.root = types.SimpleNamespace(focus_force=lambda: None)
    return h


def draft_state(rev="1", text="murky phrase", offer=True, audio=True,
                rollback=False):
    return {"draft_rev": rev, "draft": text, "draft_uncertain": [],
            "draft_offer": offer, "draft_audio": audio,
            "draft_rollback": rollback}


def test_the_retry_button_needs_the_offer_and_the_wav():
    h = make_offer_hud()

    hud.Hud._sync_draft(h, draft_state(offer=True, audio=False))
    assert h.retry_btn.packed is False
    assert h.offers.packed is False

    hud.Hud._sync_draft(h, draft_state(rev="2", offer=True, audio=True))
    assert h.retry_btn.packed is True
    assert h.rollback_btn.packed is False
    assert h.offers.packed is True


def test_the_rollback_button_shows_only_while_available():
    h = make_offer_hud()

    hud.Hud._sync_draft(h, draft_state(rollback=True))
    assert h.rollback_btn.packed is True
    assert h.offers.packed is True

    hud.Hud._sync_draft(h, draft_state(rev="2", rollback=False))
    assert h.rollback_btn.packed is False


def test_editing_hides_both_buttons_and_saving_brings_them_back():
    h = make_offer_hud()
    h.state = draft_state(rollback=True)
    hud.Hud._sync_draft(h, h.state)
    assert h.retry_btn.packed is True
    assert h.rollback_btn.packed is True

    hud.Hud._toggle_edit(h)
    assert h._draft_edit is True
    assert h.retry_btn.packed is False
    assert h.rollback_btn.packed is False

    hud.Hud._toggle_edit(h)   # save: the state still offers both
    assert h._draft_edit is False
    assert h.retry_btn.packed is True
    assert h.rollback_btn.packed is True


def test_a_click_on_a_hidden_button_does_nothing():
    h = make_offer_hud()
    h._retry_visible = False
    h._rollback_visible = False

    hud.Hud._retranscribe_draft(h)
    hud.Hud._rollback_draft(h)

    assert h.svc.retried == 0
    assert h.svc.rolled == 0


def test_a_visible_button_calls_the_service():
    h = make_offer_hud()
    h._retry_visible = True
    h._rollback_visible = True

    hud.Hud._retranscribe_draft(h)
    assert h.svc.retried == 1
    assert h.retry_btn.state == "disabled"

    hud.Hud._rollback_draft(h)
    assert h.svc.rolled == 1


# -- locales ---------------------------------------------------------------

def test_the_new_keys_exist_in_both_catalogs():
    root = Path(__file__).resolve().parents[1]
    catalogs = {}
    for lang in ("en", "ru"):
        with open(root / "app" / "locales" / f"{lang}.json",
                  encoding="utf-8") as handle:
            catalogs[lang] = json.load(handle)
    for key in ("draft.retry", "draft.rollback", "draft.retranscribed",
                "draft.rolled_back", "notify.retranscribe_no_audio",
                "notify.retranscribe_no_speech",
                "notify.retranscribe_failed"):
        assert catalogs["en"][key]
        assert catalogs["ru"][key]
    assert catalogs["ru"]["draft.retry"] == (
        "\u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u0442\u044c "
        "\u0442\u043e\u0447\u043d\u0435\u0435")
    assert catalogs["ru"]["draft.rollback"] == (
        "\u0412\u0435\u0440\u043d\u0443\u0442\u044c "
        "\u043f\u0440\u0435\u0436\u043d\u0438\u0439")
