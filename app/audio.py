"""Microphone recording with live Silero VAD auto-stop.

16 kHz, mono, float32; frames of 512 samples (32 ms) — the Silero window.
The monitor thread buffers frames, tracks speech, and closes the phrase
after silence_ms of quiet (or max_record_s hard limit).
"""
import queue
import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 512 samples
VOICE_THRESHOLD = 0.5


def load_wav_16k(path):
    """PCM WAV (16-bit, mono) -> float32 @ 16 kHz; resamples if needed.

    stdlib `wave` instead of PyAV: the live path feeds numpy arrays to
    faster-whisper and never needs av codecs.
    """
    import wave

    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sampwidth != 2:
        raise RuntimeError("нужен 16-бит PCM WAV, получен sampwidth=%d" % sampwidth)
    if channels != 1:
        raise RuntimeError("нужен моно WAV, каналов=%d" % channels)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != SAMPLE_RATE:
        x = np.arange(0, len(a), dtype=np.float32) * (SAMPLE_RATE / rate)
        a = np.interp(x, np.arange(len(a), dtype=np.float32), a).astype(np.float32)
    return a


class Recorder:
    """One recording cycle: start() -> (auto or manual) stop() -> buffer.

    Exposes live level/vad for the HUD and fires partial-transcribe
    callbacks every partial_every_s of accumulated speech.
    """

    def __init__(self, cfg, log, vad_model, on_auto_stop, on_partial=None,
                 live_state=None):
        self.cfg = cfg
        self.log = log
        self.vad = vad_model
        self.on_auto_stop = on_auto_stop  # callable(reason: str), runs in its own thread
        self.on_partial = on_partial      # callable(audio_snapshot: np.ndarray)
        self.live_state = live_state      # shared HUD dict (level/vad)
        self._q = queue.Queue(maxsize=256)
        self._frames = []
        self._lock = threading.Lock()
        self._stream = None
        self._monitor = None
        self._stop_evt = threading.Event()
        self._overruns = 0
        self._t_start = None
        self._t_last_voice = None
        self._has_speech = False
        self.level = 0.0        # RMS of the last frame (HUD)
        self.vad_p = 0.0        # last VAD speech probability (HUD)
        self._t_last_partial = 0.0

    # -- live indicators ---------------------------------------------------
    def live(self):
        """(level_rms, vad_prob, seconds_recorded) for the HUD."""
        if self._t_start is None:
            return 0.0, 0.0, 0.0
        return self.level, self.vad_p, time.monotonic() - self._t_start

    # -- lifecycle ---------------------------------------------------------
    def start(self, device_index=None):
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME,
            device=device_index,
            callback=self._callback,
        )
        self._stream.start()
        self._t_start = time.monotonic()
        self._stop_evt.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()
        self.log.info("Запись начата: device=%s", device_index)

    def stop(self):
        """Stop and return the buffer (float32, 16 kHz mono) or None."""
        self._stop_evt.set()
        if self._monitor:
            self._monitor.join(timeout=2.0)
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            if not self._frames:
                return None
            buf = np.concatenate(self._frames)
            self._frames = []
        dur = len(buf) / SAMPLE_RATE
        self.log.info(
            "Запись остановлена: %.1f с, переполнений очереди: %d", dur, self._overruns
        )
        if self.live_state is not None:
            self.live_state["level"] = 0.0
        return buf

    def had_speech(self):
        return self._has_speech

    # -- internals ---------------------------------------------------------
    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            self._overruns += 1
        try:
            self._q.put_nowait(indata.copy())
        except queue.Full:
            self._overruns += 1

    def _monitor_loop(self):
        partial_every_s = float(self.cfg.get("partial_every_s", 1.5))
        while not self._stop_evt.is_set():
            try:
                frame = self._q.get(timeout=0.1)
            except queue.Empty:
                self._check_timeouts()
                continue
            data = frame.reshape(-1)
            with self._lock:
                self._frames.append(data.copy())
            self.level = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
            prob = self._vad_prob(data)
            self.vad_p = prob
            if self.live_state is not None:
                self.live_state["level"] = self.level
                self.live_state["vad"] = self.vad_p
            if prob >= VOICE_THRESHOLD:
                self._t_last_voice = time.monotonic()
                self._has_speech = True
            self._maybe_partial(partial_every_s)
            self._check_timeouts()

    def _maybe_partial(self, partial_every_s):
        """Fire a partial transcribe every partial_every_s of live speech."""
        if self.on_partial is None or not self._has_speech:
            return
        now = time.monotonic()
        if now - self._t_last_partial < partial_every_s:
            return
        with self._lock:
            if len(self._frames) < SAMPLE_RATE // FRAME:  # < 1 s of audio
                return
            snapshot = np.concatenate(self._frames)
        self._t_last_partial = now
        threading.Thread(target=self.on_partial, args=(snapshot,), daemon=True).start()

    def _check_timeouts(self):
        now = time.monotonic()
        elapsed = now - self._t_start
        if elapsed >= self.cfg["max_record_s"]:
            self._auto_stop("limit")
            return
        if (
            self.cfg.get("auto_stop")
            and self._has_speech
            and self._t_last_voice is not None
            and (now - self._t_last_voice) * 1000.0 >= self.cfg["silence_ms"]
        ):
            self._auto_stop("silence")

    def _auto_stop(self, reason):
        if self._stop_evt.is_set():
            return
        self.log.info("Автостоп записи: %s", reason)
        self._stop_evt.set()
        threading.Thread(target=self.on_auto_stop, args=(reason,), daemon=True).start()

    def _vad_prob(self, window):
        try:
            probs = self.vad(window, num_samples=FRAME)
            return float(probs[-1])
        except Exception as e:
            self.log.debug("VAD пропущен: %s", e)
            return 0.0
