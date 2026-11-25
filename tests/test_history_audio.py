"""History with sound: the WAV beside history.jsonl and re-recognition."""
from __future__ import annotations

import json
import logging
import os
import sys
import types
import wave

import pytest

pytest.importorskip("sounddevice")

import audio
import history
import main
import numpy as np


def take(seconds: float = 0.2) -> np.ndarray:
    """A deterministic 16 kHz float32 take (a quiet tone)."""
    count = int(audio.SAMPLE_RATE * seconds)
    t = np.arange(count, dtype=np.float32) / audio.SAMPLE_RATE
    return (0.2 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)


def store(tmp_path) -> history.History:
    return history.History(str(tmp_path / "history.jsonl"))


def test_the_take_is_saved_as_a_16k_mono_wav(tmp_path):
    h = store(tmp_path)
    h.add("hello", wav=audio.wav_bytes(take()))

    record = h.load()[-1]
    path = h.record_audio_path(record)
    assert path and os.path.isfile(path)
    assert path.startswith(str(tmp_path / history.AUDIO_DIR_NAME))
    with wave.open(path, "rb") as w:
        assert w.getframerate() == audio.SAMPLE_RATE
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
    assert len(audio.load_wav_16k(path)) == len(take())


def test_a_record_without_sound_leaves_no_wav(tmp_path):
    h = store(tmp_path)
    h.add("hello")

    assert "audio" not in h.load()[-1]
    assert not (tmp_path / history.AUDIO_DIR_NAME).exists()


def test_delete_takes_the_wav_with_the_record(tmp_path):
    h = store(tmp_path)
    h.add("one", wav=audio.wav_bytes(take()))
    h.add("two", wav=audio.wav_bytes(take()))
    path = h.record_audio_path(h.load()[0])
    assert os.path.isfile(path)

    h.delete(0)

    assert not os.path.isfile(path)
    assert [r["text"] for r in h.load()] == ["two"]


def test_clear_removes_every_wav(tmp_path):
    h = store(tmp_path)
    h.add("one", wav=audio.wav_bytes(take()))
    h.add("two", wav=audio.wav_bytes(take()))
    folder = tmp_path / history.AUDIO_DIR_NAME
    assert len(list(folder.glob("*.wav"))) == 2

    h.clear()

    assert not folder.exists()


def test_turning_the_option_off_deletes_the_sound_and_keeps_the_text(tmp_path):
    h = store(tmp_path)
    h.add("one", wav=audio.wav_bytes(take()))
    h.add("two", wav=audio.wav_bytes(take()))

    h.drop_audio()

    assert not (tmp_path / history.AUDIO_DIR_NAME).exists()
    records = h.load()
    assert [r["text"] for r in records] == ["one", "two"]
    assert all("audio" not in r for r in records)


def test_the_storage_cap_drops_the_oldest_sound_only(tmp_path, monkeypatch):
    h = store(tmp_path)
    wav = audio.wav_bytes(take(0.05))
    monkeypatch.setattr(history, "MAX_AUDIO_BYTES", len(wav) * 2)
    for text in ("one", "two", "three"):
        h.add(text, wav=wav)

    records = h.load()
    assert "audio" not in records[0]
    assert records[1].get("audio") and records[2].get("audio")
    folder = tmp_path / history.AUDIO_DIR_NAME
    assert len(list(folder.glob("*.wav"))) == 2


def test_a_hand_edited_audio_path_is_ignored(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({"ts": 1, "text": "x", "chars": 1,
                                "audio": "../outside.wav"}) + "\n",
                    encoding="utf-8")
    h = history.History(str(path))

    assert h.record_audio_path(h.load()[-1]) == ""


def make_service(tmp_path, audio_on: bool, history_on: bool = True):
    svc = main.Service.__new__(main.Service)
    svc.cfg = {"history": history_on, "history_audio": audio_on,
               "locale": "en", "model": "large-v3-turbo"}
    svc.history = store(tmp_path)
    svc._target_title = ""
    svc.log = logging.getLogger("test-history-audio")
    return svc


def test_the_option_on_writes_the_wav(tmp_path):
    svc = make_service(tmp_path, audio_on=True)

    wav = svc._dictation_wav(take())
    assert wav and wav[:4] == b"RIFF"
    svc._remember("hello", wav=wav)

    path = svc.history.record_audio_path(svc.history.load()[-1])
    assert os.path.isfile(path)


def test_the_option_off_never_writes_sound(tmp_path):
    svc = make_service(tmp_path, audio_on=False)

    assert svc._dictation_wav(take()) is None
    svc._remember("hello", wav=audio.wav_bytes(take()))

    assert "audio" not in svc.history.load()[-1]
    assert not (tmp_path / history.AUDIO_DIR_NAME).exists()


def test_history_off_never_writes_sound_even_with_the_audio_option(tmp_path):
    svc = make_service(tmp_path, audio_on=True, history_on=False)

    assert svc._dictation_wav(take()) is None
    svc._remember("hello", wav=audio.wav_bytes(take()))

    assert svc.history.load() == []
    assert not (tmp_path / history.AUDIO_DIR_NAME).exists()


class NullLog:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def exception(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class FakeRecorder:
    def had_speech(self):
        return True


class FakeTranscriber:
    def transcribe(self, _buf):
        return "hello worker"


def run_a_dictation(tmp_path, monkeypatch, audio_on):
    """Drive the real worker once: recognize, insert, remember."""
    from vibeforma import service as service_mod

    monkeypatch.setattr(service_mod, "DATA_DIR", str(tmp_path))
    cfg = {"locale": "en", "dictionary": [], "snippets": [], "history": True,
           "history_audio": audio_on, "notify_text": False,
           "hud_privacy": False, "hook_command": "",
           "confirm_focus_change": False, "restore_clipboard": False,
           "paste_method": "shift_insert",
           "clipboard_exclude_history": True, "log_window_titles": False}
    svc = main.Service(cfg, NullLog())
    svc.asr = FakeTranscriber()
    svc.state = "processing"
    svc._target_hwnd = 7
    fake = types.ModuleType("insert")
    fake.get_foreground_hwnd = lambda: 7
    fake.RESULT_COPIED = "copied"
    fake.RESULT_CANCELLED = "cancelled"
    fake.RESULT_PASTED = "pasted"
    fake.insert_text = lambda *_a, **_k: "pasted"
    monkeypatch.setitem(sys.modules, "insert", fake)

    svc._transcribe_worker(take(), FakeRecorder())
    return svc


def test_a_whole_dictation_saves_sound_only_when_the_option_is_on(
        tmp_path, monkeypatch):
    off = run_a_dictation(tmp_path / "off", monkeypatch, audio_on=False)
    record = off.history.load()[-1]
    assert record["text"] == "hello worker"
    assert "audio" not in record
    assert not (tmp_path / "off" / history.AUDIO_DIR_NAME).exists()

    on = run_a_dictation(tmp_path / "on", monkeypatch, audio_on=True)
    record = on.history.load()[-1]
    assert record["text"] == "hello worker"
    path = on.history.record_audio_path(record)
    assert path and os.path.isfile(path)


def test_the_service_drops_sound_on_start_when_the_option_is_off(
        tmp_path, monkeypatch):
    from vibeforma import service as service_mod

    monkeypatch.setattr(service_mod, "DATA_DIR", str(tmp_path))
    seed = store(tmp_path)
    seed.add("hello", wav=audio.wav_bytes(take()))
    assert os.path.isfile(seed.record_audio_path(seed.load()[-1]))

    main.Service({"history": True, "history_audio": False},
                 logging.getLogger("test-history-audio"))

    assert not (tmp_path / history.AUDIO_DIR_NAME).exists()
    assert "audio" not in store(tmp_path).load()[-1]


def test_the_service_keeps_sound_on_start_when_the_option_is_on(
        tmp_path, monkeypatch):
    from vibeforma import service as service_mod

    monkeypatch.setattr(service_mod, "DATA_DIR", str(tmp_path))
    seed = store(tmp_path)
    seed.add("hello", wav=audio.wav_bytes(take()))
    path = seed.record_audio_path(seed.load()[-1])

    main.Service({"history": True, "history_audio": True},
                 logging.getLogger("test-history-audio"))

    assert os.path.isfile(path)


class FakeAsr:
    """A stand-in engine: records what it was asked, returns fixed segments."""

    calls: list[dict] = []
    built: list[dict] = []

    def __init__(self, cfg, log):
        self.cfg = dict(cfg)
        self.model = None
        FakeAsr.built.append(dict(cfg))

    def load(self):
        self.model = object()

    def transcribe_array(self, samples, duration, *, language=None,
                         initial_prompt=None):
        FakeAsr.calls.append({"model": self.cfg.get("model"),
                              "prompt": initial_prompt,
                              "language": language,
                              "samples": len(samples),
                              "duration": duration})
        return iter([types.SimpleNamespace(text="new "),
                     types.SimpleNamespace(text="text")])


def make_retranscribe_service(tmp_path, monkeypatch):
    svc = make_service(tmp_path, audio_on=True)
    svc.cfg.update({"language": "ru"})
    svc.state = "idle"
    svc.tray = None
    svc.asr = None
    svc.live = {"draft": "", "draft_rev": 0, "partial": "", "level": 0.0,
                "last": "", "last_ts": 0.0}
    svc._history_cursor = None
    svc._history_saved = ""
    svc._history_total = 0
    svc._history_saved_wav = None
    svc._draft_wav = None
    svc.history.add("old text", wav=audio.wav_bytes(take()))
    fake = types.ModuleType("asr")
    fake.ASR = FakeAsr
    monkeypatch.setitem(sys.modules, "asr", fake)
    FakeAsr.calls = []
    FakeAsr.built = []
    return svc


def test_retranscribe_last_uses_the_saved_take_with_another_model_and_prompt(
        tmp_path, monkeypatch):
    svc = make_retranscribe_service(tmp_path, monkeypatch)

    text = svc.retranscribe_last("small", "new prompt")

    assert text == "new text"
    assert svc.live["draft"] == "new text"
    assert fake_last_call() == {"model": "small", "prompt": "new prompt",
                                "language": "ru",
                                "samples": len(take()),
                                "duration": pytest.approx(len(take()) / 16000.0)}
    assert svc.live["last"] == svc.tr("history.retranscribed", n=8)


def fake_last_call() -> dict:
    assert FakeAsr.calls, "the temporary engine was never asked"
    return FakeAsr.calls[-1]


def test_retranscribe_last_defaults_to_the_configured_model(tmp_path,
                                                           monkeypatch):
    svc = make_retranscribe_service(tmp_path, monkeypatch)

    svc.retranscribe_last()

    assert FakeAsr.built[-1]["model"] == "large-v3-turbo"
    assert fake_last_call()["model"] == "large-v3-turbo"
    assert fake_last_call()["prompt"] is None


def test_retranscribe_last_without_saved_sound_says_so(tmp_path, monkeypatch):
    svc = make_retranscribe_service(tmp_path, monkeypatch)
    svc.history.add("text only")

    assert svc.retranscribe_last() == ""

    assert svc.live["last"] == svc.tr("history.no_audio")
    assert FakeAsr.calls == []
    assert FakeAsr.built == []
