"""Recording cues: a real WAV through the output device, Beep as the fallback."""
import sys
import wave

import pytest
from vibeforma import util


class _FakeWinsound:
    """Stands in for the winsound module; records what was played."""

    SND_FILENAME = 0x00020000
    SND_ASYNC = 0x0001
    SND_NODEFAULT = 0x0002

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.played: list[tuple[str, int]] = []
        self.beeps: list[tuple[int, int]] = []

    def PlaySound(self, path: str, flags: int) -> None:
        if self.fail:
            raise RuntimeError("no output device")
        self.played.append((path, flags))

    def Beep(self, freq: int, dur_ms: int) -> None:
        self.beeps.append((freq, dur_ms))


class _InlineThread:
    """Runs the target at start(), so a threaded cue is testable."""

    def __init__(self, target, daemon=None) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


@pytest.fixture
def fake_winsound(monkeypatch, tmp_path):
    fake = _FakeWinsound()
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(util.tempfile, "gettempdir", lambda: str(tmp_path))
    return fake


def test_the_start_cue_plays_a_two_note_wav(fake_winsound):
    util.cue("start")

    assert len(fake_winsound.played) == 1
    path, flags = fake_winsound.played[0]
    assert flags & fake_winsound.SND_FILENAME
    assert flags & fake_winsound.SND_ASYNC
    assert flags & fake_winsound.SND_NODEFAULT
    with wave.open(path, "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == util._CUE_RATE
        note = int(util._CUE_RATE * util._CUE_NOTE_MS / 1000)
        assert wav.getnframes() == 2 * note


def test_the_stop_cue_is_a_different_file(fake_winsound):
    util.cue("start")
    util.cue("stop")

    start_path = fake_winsound.played[0][0]
    stop_path = fake_winsound.played[1][0]
    assert start_path != stop_path
    assert "start" in start_path
    assert "stop" in stop_path


def test_a_failing_playback_falls_back_to_the_pc_speaker(monkeypatch, tmp_path):
    fake = _FakeWinsound(fail=True)
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(util.tempfile, "gettempdir", lambda: str(tmp_path))
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(util, "beep", lambda freq, dur: calls.append((freq, dur)))

    util.cue("stop")

    assert calls == [(900, 120)]


def test_an_unknown_cue_is_a_programming_error(fake_winsound):
    with pytest.raises(ValueError):
        util.cue("pause")
    assert fake_winsound.played == []


def test_beep_plays_a_wav_instead_of_the_pc_speaker(fake_winsound, monkeypatch):
    monkeypatch.setattr(util.threading, "Thread", _InlineThread)

    util.beep(440, 50)

    assert fake_winsound.beeps == []
    path, flags = fake_winsound.played[-1]
    assert "beep-440" in path
    assert flags & fake_winsound.SND_FILENAME
