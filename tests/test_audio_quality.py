"""Audio capture quality: VAD padding, the resampler, refresh, system audio."""
import logging
import threading
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


def test_system_source_records_and_never_auto_stops(monkeypatch, tmp_path):
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
               max_record_s=10, system_recordings_dir=str(tmp_path))
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


def test_safe_stop_slow_receiver_keeps_chunk_out_of_the_buffer():
    """A monitor hung in the chunk handoff past the 2 s join timeout must
    not resurrect the delivered audio in stop(), and the frames captured
    after the chunk cut must survive in the buffer stop() returns."""
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    entered = threading.Event()
    release = threading.Event()
    delivered = []

    def slow_receiver(chunk):
        delivered.append(chunk)
        entered.set()
        release.wait(10.0)
        return True

    def vad(window, num_samples=0):
        voiced = float(np.abs(window).max()) > 0.1
        return np.array([1.0 if voiced else 0.0], dtype=np.float32)

    rec = audio.Recorder(_cfg(continuous=True),
                         logging.getLogger("test_audio_quality"),
                         vad, _no_stop, on_chunk=slow_receiver)
    rec._t_start = time.monotonic()
    rec._continuous = True
    rec._stop_evt.clear()
    rec._monitor = threading.Thread(target=rec._monitor_loop, daemon=True)
    rec._monitor.start()
    try:
        frame = audio.FRAME
        loud = np.full(frame, 0.5, dtype=np.float32)
        quiet = np.zeros(frame, dtype=np.float32)
        # 1.28 s of speech, then a 0.64 s pause: the chunk cuts at the last
        # voiced frame and the receiver hangs on it. The 13 quiet frames the
        # monitor buffered before the cut stay behind and belong to stop().
        for _ in range(40):
            rec._q.put(loud)
        for _ in range(20):
            rec._q.put(quiet)
        assert entered.wait(timeout=5.0), "the chunk was never handed off"

        started = time.monotonic()
        buf = rec.stop()
        elapsed = time.monotonic() - started
        # The hung monitor must not make stop() wait forever.
        assert elapsed < 5.0
        assert buf is not None
        # The delivered chunk is not in the final buffer: no doubled words.
        assert float(np.abs(buf).max()) < 0.1
        # The frames captured after the chunk delivery are not lost.
        assert len(buf) == 13 * frame
        assert len(delivered) == 1
        assert len(delivered[0]) == 40 * frame

        # The receiver finally accepts: the buffer must stay drained.
        release.set()
        rec._monitor.join(timeout=5.0)
        assert not rec._monitor.is_alive()
        assert rec._frames == []
    finally:
        release.set()


def test_safe_stop_closes_the_stream_when_stop_raises():
    """A stream whose stop() raises (dead or re-created device) must still
    be closed exactly once, or the device stays busy process-wide."""
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")

    class FakeStream:
        def __init__(self):
            self.stop_calls = 0
            self.close_calls = 0

        def stop(self):
            self.stop_calls += 1
            raise RuntimeError("device lost")

        def close(self):
            self.close_calls += 1

    stream = FakeStream()
    rec = audio.Recorder(_cfg(), logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop)
    rec._stream = stream
    assert rec.stop() is None
    assert stream.stop_calls == 1
    assert stream.close_calls == 1
    assert rec._stream is None


def _loud_vad(window, num_samples=0):
    voiced = float(np.abs(window).max()) > 0.1
    return np.array([1.0 if voiced else 0.0], dtype=np.float32)


class _GatedThread(threading.Thread):
    """A real thread whose partial delivery is held until the test allows it.

    Only threads targeting Recorder._run_partial are gated, so monitors and
    helpers keep running normally. This makes "the partial thread outlives
    stop()" deterministic: the thread is spawned, then blocked before its
    target runs, and released only after stop() has returned.
    """

    registry = None   # list of gated threads, set per test
    gate = None       # threading.Event, set per test

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        target = kwargs.get("target")
        if getattr(target, "__name__", "") == "_run_partial":
            type(self).registry.append(self)

    def run(self):
        if self in type(self).registry:
            type(self).gate.wait(5.0)
        super().run()


def _use_gated_partial_threads(monkeypatch, audio):
    registry, gate = [], threading.Event()
    gated = type("GatedThread", (_GatedThread,),
                 {"registry": registry, "gate": gate})
    monkeypatch.setattr(audio.threading, "Thread", gated)
    return registry, gate


def test_monitor_partial_survives_unexpected_error(monkeypatch, caplog):
    """An unexpected failure in one monitor iteration must not kill the
    thread silently: the error is logged with a traceback, and chunk cuts
    plus the silence auto-stop keep working on the frames that follow."""
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    stopped = []
    chunks = []
    ingested = {"n": 0}
    real_ingest = audio.Recorder._ingest

    def flaky_ingest(self, data):
        ingested["n"] += 1
        if ingested["n"] == 2:
            raise RuntimeError("boom")
        return real_ingest(self, data)

    monkeypatch.setattr(audio.Recorder, "_ingest", flaky_ingest)
    cfg = _cfg(continuous=True, auto_stop=True, silence_ms=600)
    rec = audio.Recorder(cfg, logging.getLogger("test_audio_quality"),
                         _loud_vad, stopped.append, on_chunk=chunks.append)
    rec._t_start = time.monotonic()
    rec._continuous = True
    rec._stop_evt.clear()
    frame = audio.FRAME
    loud = np.full(frame, 0.5, dtype=np.float32)
    quiet = np.zeros(frame, dtype=np.float32)
    with caplog.at_level(logging.ERROR):
        rec._monitor = threading.Thread(target=rec._monitor_loop, daemon=True)
        rec._monitor.start()
        try:
            # 1.28 s of speech (the 2nd frame dies in _ingest), then a long
            # pause: the chunk cuts at the seam and the silence auto-stop
            # closes the take once 600 ms of wall time have passed.
            for _ in range(40):
                rec._q.put(loud)
            for _ in range(30):
                rec._q.put(quiet)
            deadline = time.time() + 10.0
            while not stopped and time.time() < deadline:
                time.sleep(0.02)
            # The monitor kept iterating after the bad frame.
            assert ingested["n"] == 70
        finally:
            rec.stop()

    assert not rec._monitor.is_alive()
    failures = [r for r in caplog.records
                if r.levelno >= logging.ERROR and r.exc_info]
    assert any("Monitor iteration failed" in r.getMessage() for r in failures)
    assert any("RuntimeError: boom" in
               logging.Formatter().formatException(r.exc_info)
               for r in failures)
    # Chunking survived: the cut lands on the last voiced frame (39 frames,
    # the poisoned one never reached the buffer).
    assert len(chunks) == 1
    assert len(chunks[0]) == 39 * frame
    # The silence auto-stop survived.
    assert stopped == ["silence"]


def test_monitor_partial_stale_thread_does_not_publish_after_stop(monkeypatch):
    """A partial thread spawned before stop() whose work outlives the stop
    must not publish its snapshot once stop() has returned."""
    audio = pytest.importorskip("audio")
    pytest.importorskip("sounddevice")
    published = []
    registry, gate = _use_gated_partial_threads(monkeypatch, audio)
    rec = audio.Recorder(_cfg(), logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop, on_partial=published.append)
    rec._t_start = time.monotonic()
    rec._has_speech = True
    rec._t_last_partial = 0.0
    rec._frames = [np.full(audio.FRAME, 0.25, dtype=np.float32)
                   for _ in range(100)]
    try:
        rec._maybe_partial(1.5)
        assert len(registry) == 1
        # The thread is alive but held before its target: it outlives stop().
        buf = rec.stop()
        assert buf is not None
        gate.set()
        registry[0].join(timeout=5.0)
        assert not registry[0].is_alive()
        assert published == []
    finally:
        gate.set()


def test_monitor_partial_new_take_gets_only_its_own(monkeypatch):
    """On a fast repeated take the stale partial of the previous take stays
    unpublished while the new take's own partial reaches the consumer."""
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
    published = []
    registry, gate = _use_gated_partial_threads(monkeypatch, audio)
    rec = audio.Recorder(_cfg(), logging.getLogger("test_audio_quality"),
                         _fake_vad, _no_stop, on_partial=published.append)
    try:
        # Take 1: a partial thread is spawned but held before delivery.
        rec._t_start = time.monotonic()
        rec._has_speech = True
        rec._t_last_partial = 0.0
        rec._frames = [np.full(audio.FRAME, 0.25, dtype=np.float32)
                       for _ in range(100)]
        rec._maybe_partial(1.5)
        assert len(registry) == 1
        rec.stop()

        # Take 2 on the same Recorder, started before the stale thread ran.
        rec.start()
        rec._has_speech = True
        rec._t_last_partial = 0.0
        rec._frames = [np.full(audio.FRAME, 0.75, dtype=np.float32)
                       for _ in range(100)]
        rec._maybe_partial(1.5)
        assert len(registry) == 2

        gate.set()
        for thread in registry:
            thread.join(timeout=5.0)
        # Only take 2's own partial is published; the stale one is dropped.
        assert len(published) == 1
        assert float(np.mean(published[0])) == pytest.approx(0.75)
        rec.stop()
    finally:
        gate.set()
        rec.stop()
