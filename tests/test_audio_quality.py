"""Audio capture quality: VAD padding, the resampler, refresh, system audio."""
import logging
import time

import pytest

np = pytest.importorskip("numpy")

SAMPLE_RATE = 16000
RATE = 48000
VAD_EXPECTED = {"min_silence_duration_ms": 1000, "speech_pad_ms": 400}


def _cfg(**over):
    cfg = {
        "language": "en",
        "hotwords": "",
        "dictionary": [],
        "max_record_s": 120,
        "silence_ms": 2000,
        "auto_stop": True,
        "partial_every_s": 1.5,
        "audio_source": "microphone",
    }
    cfg.update(over)
    return cfg


def _tone(freq, seconds=0.25, rate=RATE):
    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _rms(values):
    return float(np.sqrt(np.mean(np.asarray(values, dtype=np.float64) ** 2)))


def _fake_vad(_window, num_samples=0):
    return np.zeros(1, dtype=np.float32)


def _no_stop(_reason):
    return None


def test_vad_padding_keeps_the_onset():
    asr = pytest.importorskip("asr")
    assert asr.VAD_PARAMETERS == VAD_EXPECTED


def test_transcribe_passes_the_vad_options():
    asr = pytest.importorskip("asr")
    captured = {}

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            captured["audio"] = audio
            captured.update(kwargs)
            return [], None

    engine = asr.ASR(_cfg(), logging.getLogger("test_audio_quality"))
    engine.model = FakeModel()
    engine._transcribe_impl(np.zeros(SAMPLE_RATE, dtype=np.float32), "en")
    assert captured["vad_parameters"] == VAD_EXPECTED
    assert captured["vad_filter"] is True
    assert captured["beam_size"] == 5


def test_resampler_keeps_the_speech_band():
    audio = pytest.importorskip("audio")
    for freq, floor in ((1000, 0.99), (5000, 0.95), (6000, 0.90)):
        source = _tone(freq)
        out = audio.Resampler(RATE).process(source)
        got = _rms(out[400:-400]) / _rms(source[400:-400])
        assert got >= floor, f"{freq} Hz is kept at {got:.3f}, below {floor}"


def test_resampler_rejects_aliasing():
    audio = pytest.importorskip("audio")
    source = _tone(9000)
    out = audio.Resampler(RATE).process(source)[400:-400]
    spectrum = np.abs(np.fft.rfft(out)) ** 2
    freqs = np.fft.rfftfreq(out.size, 1.0 / SAMPLE_RATE)
    folded = spectrum[(freqs >= 6000) & (freqs <= 8000)].sum()
    alias_amplitude = 2.0 * np.sqrt(folded) / out.size
    assert alias_amplitude / _rms(source[400:-400]) <= 0.02


def test_refresh_devices_reinitializes(monkeypatch):
    audio = pytest.importorskip("audio")
    calls = {"terminate": 0, "initialize": 0}

    def terminate():
        calls["terminate"] += 1

    def initialize():
        calls["initialize"] += 1

    monkeypatch.setattr(audio.sd, "_terminate", terminate)
    monkeypatch.setattr(audio.sd, "_initialize", initialize)
    assert audio.refresh_devices() is True
    assert calls == {"terminate": 1, "initialize": 1}


def test_recorder_retries_after_refresh(monkeypatch):
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    state = {"refreshed": 0, "opens": 0}

    def fake_refresh():
        state["refreshed"] += 1
        return True

    def fake_open(self, device, rate, channels):
        # The stale endpoint refuses every plan, including the 0.25 s retry;
        # only the re-enumeration after refresh_devices() makes it open.
        state["opens"] += 1
        if not state["refreshed"]:
            return False
        self._stream = None
        self._rate = rate
        self._resampler = None
        return True

    monkeypatch.setattr(audio, "capture_plans",
                        lambda device_index=None: [(1, RATE, 1)])
    monkeypatch.setattr(audio, "refresh_devices", fake_refresh)
    monkeypatch.setattr(audio.Recorder, "_open", fake_open)
    rec = audio.Recorder(_cfg(), logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop)
    rec.start()
    assert state["refreshed"] == 1
    assert state["opens"] >= 3
    rec.stop()


def test_system_source_records_and_never_auto_stops(monkeypatch):
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    system_audio = pytest.importorskip("system_audio")

    class FakeCapture:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True
            return False

        def record(self, numframes):
            time.sleep(0.005)
            if self.closed:
                raise RuntimeError("the loopback recorder is closed")
            return np.zeros((numframes, 2), dtype=np.float32)

    class FakeMic:
        def recorder(self, samplerate, channels):
            assert samplerate == RATE
            assert channels == 2
            return FakeCapture()

    monkeypatch.setattr(system_audio, "default_loopback",
                        lambda: (FakeMic(), "Fake output"))
    cfg = _cfg(audio_source="system", auto_stop=True, silence_ms=50,
               max_record_s=10)
    rec = audio.Recorder(cfg, logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop)
    rec.start()
    time.sleep(0.2)
    assert not rec._stop_evt.is_set()
    buf = rec.stop()
    assert buf is not None
    assert buf.dtype == np.float32
    assert len(buf) > 0
    assert len(buf) % audio.FRAME == 0


def test_system_take_ignores_max_record_s():
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    stopped = []
    cfg = _cfg(audio_source="system", auto_stop=True, silence_ms=50,
               max_record_s=10)
    rec = audio.Recorder(cfg, logging.getLogger("test_audio_quality"),
                         _fake_vad, stopped.append)
    rec._source = "system"
    rec._continuous = False
    rec._t_start = time.monotonic() - 1000.0
    rec._has_speech = True
    rec._t_last_voice = time.monotonic() - 100.0

    rec._check_timeouts()

    assert stopped == []
    assert not rec._stop_evt.is_set()


def test_system_partial_fires():
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    calls = []
    cfg = _cfg(audio_source="system", partial_every_s=1.5)
    rec = audio.Recorder(cfg, logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop, on_partial=calls.append)
    rec._source = "system"
    rec._has_speech = True
    rec._t_last_partial = 0.0
    rec._frames = [np.zeros(audio.FRAME, dtype=np.float32) for _ in range(100)]

    rec._maybe_partial(1.5)

    deadline = time.time() + 2.0
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls
    assert len(calls[0]) <= int(audio.PARTIAL_WINDOW_S * SAMPLE_RATE)


def test_system_frames_stay_within_the_buffer():
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    limit = int(audio.SYSTEM_BUFFER_S * SAMPLE_RATE) // audio.FRAME
    system = audio.Recorder(_cfg(audio_source="system"),
                            logging.getLogger("test_audio_quality"),
                            _fake_vad, _no_stop)
    system._source = "system"
    for _ in range(limit + 300):
        system._ingest(np.zeros(audio.FRAME, dtype=np.float32))
    assert len(system._frames) <= limit

    # The microphone path is untouched: its buffer is the chunk path's.
    mic = audio.Recorder(_cfg(audio_source="microphone"),
                         logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop)
    mic._t_start = time.monotonic()
    for _ in range(limit + 300):
        mic._ingest(np.zeros(audio.FRAME, dtype=np.float32))
    assert len(mic._frames) == limit + 300


def test_microphone_take_resets_the_source_after_system(monkeypatch):
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")

    def fake_open(self, device, rate, channels):
        self._stream = None
        self._rate = rate
        self._resampler = None
        return True

    monkeypatch.setattr(audio, "capture_plans",
                        lambda device_index=None: [(1, RATE, 1)])
    monkeypatch.setattr(audio.Recorder, "_open", fake_open)
    rec = audio.Recorder(_cfg(audio_source="microphone"),
                         logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop)
    rec._source = "system"  # the previous take on this same Recorder
    rec.start()
    assert rec._source == "microphone"
    rec.stop()


def test_system_audio_unavailable_is_clear(monkeypatch):
    system_audio = pytest.importorskip("system_audio")
    monkeypatch.setattr(system_audio, "sc", None)
    with pytest.raises(system_audio.SystemAudioUnavailable) as info:
        system_audio.default_loopback()
    assert "soundcard" in str(info.value)


class _FakeInfo:
    language = "en"


class _CapturingModel:
    """Records one transcribe() call; yields no text."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return [], _FakeInfo()


def test_final_dictation_uses_the_short_temperature_ladder():
    asr = pytest.importorskip("asr")
    engine = asr.ASR(_cfg(), logging.getLogger("test_audio_quality"))
    engine.model = _CapturingModel()
    engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
    _audio, kwargs = engine.model.calls[-1]
    assert kwargs["beam_size"] == 5
    assert kwargs["temperature"] == (0.0, 0.2)
    assert kwargs["vad_filter"] is True
    assert kwargs["condition_on_previous_text"] is False


def test_partial_uses_greedy_decoding():
    asr = pytest.importorskip("asr")
    engine = asr.ASR(_cfg(), logging.getLogger("test_audio_quality"))
    engine.model = _CapturingModel()
    engine.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                      verbose=False, partial=True)
    _audio, kwargs = engine.model.calls[-1]
    assert kwargs["beam_size"] == 1
    assert kwargs["temperature"] == 0.0
    assert kwargs["vad_filter"] is True
    assert kwargs["condition_on_previous_text"] is False


def test_partial_snapshot_keeps_only_the_window():
    audio = pytest.importorskip("audio")
    frames = [np.zeros(audio.FRAME, dtype=np.float32) for _ in range(1000)]
    frames[-1][-1] = 1.0
    snapshot = audio.partial_snapshot(frames)
    assert 0 < len(snapshot) <= int(audio.PARTIAL_WINDOW_S * SAMPLE_RATE)
    assert len(snapshot) < 1000 * audio.FRAME
    assert snapshot[-1] == 1.0
