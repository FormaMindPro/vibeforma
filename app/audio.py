"""Microphone recording with live Silero VAD auto-stop.

16 kHz, mono, float32; frames of 512 samples (32 ms) — the Silero window.
The monitor thread buffers frames, tracks speech, and closes the phrase
after silence_ms of quiet (or max_record_s hard limit). In continuous
dictation the hard limit is off: the take is cut into bounded chunks at
pauses and handed to the caller for piecewise recognition.
"""
import contextlib
import queue
import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 512 samples
PARTIAL_WINDOW_S = 10.0   # a live partial reads only the tail of the take
VOICE_THRESHOLD = 0.5
# Continuous dictation: a take is not cut by max_record_s. It is cut into
# bounded chunks at a pause after speech, or at the chunk cap when the
# speaker runs on; the chunk cap is the Whisper window, so one recognition
# pass never decodes more. The overlap after a hard cut carries the last
# frames into the next chunk, so a word split by the boundary is complete in
# the next recognition; the text side removes the repeated words.
CONTINUOUS_CHUNK_S = 30.0
CONTINUOUS_PAUSE_MS = 400
CONTINUOUS_MIN_S = 1.0
CONTINUOUS_OVERLAP_S = 0.3
CONTINUOUS_HARD_MAX_S = 240.0
# System audio: the loopback is captured at the output's native rate and goes
# through the same Resampler. 48 kHz is the shared-mode mix format on Windows.
SYSTEM_RATE = 48000
SYSTEM_RATES = (48000, 44100, 16000)
SYSTEM_BLOCK = SYSTEM_RATE * FRAME_MS // 1000
# System audio for live subtitles (F9): a film has no phrase ends to cut on
# and no natural end of its own, so the take runs until the hotkey. The
# buffer is bounded — the plate reads only the last PARTIAL_WINDOW_S — and a
# partial recognition runs at most this often, never on every partial_every_s.
SYSTEM_BUFFER_S = 30.0
SYSTEM_PARTIAL_EVERY_S = 4.0


class Resampler:
    """Stateful band-limited resampler (windowed sinc), numpy only.

    A device usually runs at 44.1 or 48 kHz. Asking the legacy host API for
    16 kHz makes Windows convert with its old resampler, and the folded high
    frequencies land in the speech band: heard as a lisp, read by the model
    as wrong words. Capturing at the native rate and filtering here keeps the
    band clean.
    """

    def __init__(self, src_rate, dst_rate=SAMPLE_RATE, taps=127, beta=10.0):
        self.src_rate = float(src_rate)
        self.dst_rate = float(dst_rate)
        self.step = self.src_rate / self.dst_rate
        if taps % 2 == 0:
            taps += 1
        self.taps = taps
        self.half = taps // 2
        # Stop below the lower Nyquist, with a margin for the window's own
        # transition band.
        self.cutoff = min(1.0, self.dst_rate / self.src_rate) * 0.94
        self.window = np.kaiser(taps, beta)
        self.buf = np.zeros(self.half, dtype=np.float32)
        self.pos = float(self.half)
        self._idx = np.arange(taps)

    def process(self, x):
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if not x.size:
            return np.zeros(0, dtype=np.float32)
        self.buf = np.concatenate((self.buf, x))
        n = self.buf.size
        limit = n - 1 - self.half
        count = int(max(0, np.floor((limit - self.pos) / self.step) + 1))
        if count <= 0:
            return np.zeros(0, dtype=np.float32)
        p = self.pos + self.step * np.arange(count)
        base = np.floor(p).astype(np.int64)
        idx = base[:, None] - self.half + self._idx[None, :]
        frac = p[:, None] - idx
        weights = self.cutoff * np.sinc(self.cutoff * frac) * self.window[None, :]
        out = (self.buf[idx] * weights).sum(axis=1) / weights.sum(axis=1)
        self.pos = p[-1] + self.step
        keep = max(0, int(np.floor(self.pos)) - self.half - 1)
        if keep > 0:
            self.buf = self.buf[keep:]
            self.pos -= keep
        return out.astype(np.float32)


def resample_16k(audio, rate):
    """Whole buffer -> float32 @ 16 kHz; a no-op at 16 kHz."""
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    if int(round(rate)) == SAMPLE_RATE:
        return a
    r = Resampler(rate)
    tail = np.zeros(r.taps, dtype=np.float32)   # flush the filter's look-ahead
    return r.process(np.concatenate((a, tail)))


_REFRESH_LOCK = threading.Lock()


def refresh_devices() -> bool:
    """Re-enumerate PortAudio devices after an endpoint change.

    A long-running process keeps the device table it read at startup. When
    Windows re-creates an endpoint (driver restart, jack or Bluetooth
    change), the cached entry fails with an unanticipated host error and
    every take falls back to the legacy path. No stream is open when this
    is called. Returns False if the re-initialization raised.
    """
    with _REFRESH_LOCK:
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - the caller retries the plans
            return False
    return True


def capture_plans(device_index=None):
    """Ordered (device, rate, channels) attempts, best first.

    WASAPI shared mode rejects 16 kHz and hands 48 kHz. Its driver can refuse
    a stream once while the device is busy (a known WDM IOCTL error), so the
    next attempts are: the same device in stereo, then the same microphone on
    its original host API at its native rate, and only then the old 16 kHz
    route. Every attempt above 16 kHz goes through Resampler, so even the
    legacy host API no longer converts with the old Windows resampler.
    """
    import sounddevice as sd

    if device_index is None:
        device_index = sd.default.device[0]
    plans = []
    wasapi_dev = None
    wasapi_rate = None
    try:
        dev = sd.query_devices(device_index)
    except Exception:  # noqa: BLE001 - an unknown index falls back to the old path
        return [(device_index, SAMPLE_RATE, 1)]
    rate = float(dev.get("default_samplerate") or SAMPLE_RATE)
    apis = sd.query_hostapis()
    wasapi = next((i for i, a in enumerate(apis)
                   if a.get("name") == "Windows WASAPI"), None)
    if wasapi is not None and dev.get("hostapi") != wasapi:
        for i in apis[wasapi].get("devices", []):
            try:
                cand = sd.query_devices(i)
            except Exception:  # noqa: BLE001, S112 - a stale index is not fatal
                continue
            if (cand.get("max_input_channels", 0) > 0
                    and str(cand.get("name", "")).lower()
                    == str(dev.get("name", "")).lower()):
                wasapi_dev = i
                wasapi_rate = float(cand.get("default_samplerate") or rate)
                break
    if wasapi_dev is not None:
        plans.append((wasapi_dev, wasapi_rate, 1))
        if wasapi_rate >= 32000:
            plans.append((wasapi_dev, wasapi_rate, 2))
    plans.append((device_index, rate, 1))
    plans.append((device_index, SAMPLE_RATE, 1))
    seen, out = set(), []
    for plan in plans:
        if plan not in seen:
            seen.add(plan)
            out.append(plan)
    return out


def wav_bytes(audio, rate=SAMPLE_RATE):
    """float32 mono samples -> 16-bit PCM WAV bytes.

    The mirror of ``load_wav_16k``: what a dictation take is saved as beside
    the history entry, and what the same take is read back from when it is
    re-recognized. Clipping keeps a hot take from wrapping around the int16
    range; the level is otherwise untouched.
    """
    import io
    import wave

    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())
    return out.getvalue()


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
        raise RuntimeError(
            f"16-bit PCM WAV required, got sampwidth={sampwidth}")
    if channels != 1:
        raise RuntimeError(f"mono WAV required, channels={channels}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return resample_16k(a, rate)


def partial_snapshot(frames, window_s=PARTIAL_WINDOW_S):
    """The last ``window_s`` seconds of captured frames.

    A partial is a preview, not the final take: decoding the whole growing
    buffer on every tick makes the engine lock wait grow with the take.
    Returns a new float32 array; ``frames`` is never modified.
    """
    if not frames:
        return np.zeros(0, dtype=np.float32)
    limit = max(1, int(window_s * SAMPLE_RATE))
    kept = []
    total = 0
    for frame in reversed(frames):
        kept.append(frame)
        total += len(frame)
        if total >= limit:
            break
    kept.reverse()
    audio = np.concatenate(kept)
    return audio[-limit:]


class Recorder:
    """One recording cycle: start() -> (auto or manual) stop() -> buffer.

    Exposes live level/vad for the HUD and fires partial-transcribe
    callbacks every partial_every_s of accumulated speech.
    """

    def __init__(self, cfg, log, vad_model, on_auto_stop, on_partial=None,
                 live_state=None, on_chunk=None, keep_take=False):
        self.cfg = cfg
        self.log = log
        self.vad = vad_model
        self.on_auto_stop = on_auto_stop  # callable(reason: str), runs in its own thread
        self.on_partial = on_partial      # callable(audio_snapshot: np.ndarray)
        # callable(chunk: np.ndarray) -> bool; False means "no room, keep it"
        self.on_chunk = on_chunk
        # History with sound needs the whole take: the chunk cuts then keep a
        # copy (opt-in, documented as the one place the take is not trimmed).
        self._keep_take = keep_take
        self._take_frames = []
        self.live_state = live_state      # shared HUD dict (level/vad)
        self._q = queue.Queue(maxsize=256)
        self._frames = []
        self._lock = threading.Lock()
        self._stream = None
        self._monitor = None
        self._stop_evt = threading.Event()
        self._overruns = 0
        self._rate = SAMPLE_RATE
        self._resampler = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._t_start = None
        self._t_last_voice = None
        self._has_speech = False
        self.level = 0.0        # RMS of the last frame (HUD)
        self.vad_p = 0.0        # last VAD speech probability (HUD)
        self._t_last_partial = 0.0
        # Continuous chunk bookkeeping, reset per take (see _reset_take).
        self._continuous = False
        self._chunk_samples = 0     # samples in the current buffer
        self._voice_end = 0         # buffer samples up to the last voiced frame
        self._quiet_samples = 0     # buffer samples since the last voiced frame
        self._voiced_samples = 0    # speech samples in the current chunk
        self._chunk_deferred = False
        self._source = "microphone"     # microphone | system
        self._system_rec = None
        self._system_thread = None
        self._system_rate = SYSTEM_RATE
        self._system_block = SYSTEM_BLOCK

    # -- live indicators ---------------------------------------------------
    def live(self):
        """(level_rms, vad_prob, seconds_recorded) for the HUD."""
        if self._t_start is None:
            return 0.0, 0.0, 0.0
        return self.level, self.vad_p, time.monotonic() - self._t_start

    # -- lifecycle ---------------------------------------------------------
    def _open(self, device, rate, channels):
        """One capture attempt; on success the stream and the filter are set."""
        block = (FRAME if int(round(rate)) == SAMPLE_RATE
                 else max(FRAME, int(round(rate * FRAME_MS / 1000))))
        try:
            stream = sd.InputStream(
                samplerate=rate,
                channels=channels,
                dtype="float32",
                blocksize=block,
                device=device,
                callback=self._callback,
            )
            stream.start()
        except Exception as e:  # noqa: BLE001 - try the next plan, never raise here
            # One line, not a traceback: the reason is what matters when the
            # modern path falls back.
            self.log.info("Capture %s Hz x%d on device %s failed: %s",
                          rate, channels, device, e)
            return False
        self._stream = stream
        self._rate = rate
        self._resampler = (None if int(round(rate)) == SAMPLE_RATE
                           else Resampler(rate))
        return True

    def _reset_take(self):
        """Per-take state: the buffer, the speech flags and the chunk counters.

        A Recorder outlives a take (the service reuses the setting object), so
        a stale speech flag from the previous phrase must not close this one.
        """
        self._frames = []
        self._take_frames = []
        self._has_speech = False
        self._t_last_voice = None
        self._chunk_samples = 0
        self._voice_end = 0
        self._quiet_samples = 0
        self._voiced_samples = 0
        self._chunk_deferred = False
        self._continuous = bool(self.cfg.get("continuous", True))
        if self._continuous and self.on_chunk is None:
            # Without a receiver the take could not be recognized piecewise;
            # the old hard limit stays, and the log says why.
            self.log.info("Continuous dictation has no chunk receiver — "
                          "max_record_s applies")
            self._continuous = False

    def start(self, device_index=None):
        self._pending = np.zeros(0, dtype=np.float32)
        self._reset_take()
        # The same Recorder outlives a setting change; a previous system take
        # must not leave the silence auto-stop and partials disabled here.
        self._source = "microphone"
        if str(self.cfg.get("audio_source") or "microphone") == "system":
            self._start_system()
            return
        chosen = self._open_plans(device_index)
        if chosen is None:
            # A long-running process keeps the device table it read at
            # startup; when Windows re-created the endpoint every plan fails.
            # Re-enumerate once and try the same plans again.
            refresh_devices()
            chosen = self._open_plans(device_index)
        if chosen is None:
            raise RuntimeError("no capture device could be opened")
        self._t_start = time.monotonic()
        self._stop_evt.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()
        api = "unknown"
        with contextlib.suppress(Exception):
            dev_info = sd.query_devices(chosen[0])
            api = sd.query_hostapis()[dev_info["hostapi"]]["name"]
        self.log.info("Recording started: device=%s (%s) rate=%s Hz x%d api=%s",
                      chosen[0], device_index, chosen[1], chosen[2], api)

    def _open_plans(self, device_index):
        """First plan that opens; the first plan gets one 0.25 s retry."""
        for index, (dev, rate, channels) in enumerate(capture_plans(device_index)):
            if self._open(dev, rate, channels):
                return (dev, rate, channels)
            # A driver that is re-enumerating the endpoint refuses one stream
            # with a WDM IOCTL error and accepts the next: a short pause is
            # cheaper than losing the clean path for the whole take.
            if index == 0:
                time.sleep(0.25)
                if self._open(dev, rate, channels):
                    return (dev, rate, channels)
        return None

    def _start_system(self):
        """Take what the default output plays, through the soundcard loopback."""
        from system_audio import default_loopback

        self._reset_take()
        # System audio has no pauses to cut on and no dictation end: its own
        # long-capture path is live subtitles (F9) — no chunk cuts, no
        # auto-stop, no max_record_s; the take ends by the hotkey.
        self._continuous = False

        mic, name = default_loopback()
        last = None
        for rate in SYSTEM_RATES:
            try:
                rec = mic.recorder(samplerate=rate, channels=2)
                rec.__enter__()
            except Exception as e:  # noqa: BLE001 - the next rate may open
                last = e
                continue
            self._system_rec = rec
            self._system_rate = rate
            self._rate = rate
            self._system_block = max(FRAME, int(round(rate * FRAME_MS / 1000)))
            self._resampler = None if rate == SAMPLE_RATE else Resampler(rate)
            break
        if self._system_rec is None:
            raise RuntimeError("system audio could not be opened: " + str(last))
        self._source = "system"
        self._t_start = time.monotonic()
        self._stop_evt.clear()
        self._system_thread = threading.Thread(target=self._system_loop,
                                               daemon=True)
        self._system_thread.start()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()
        self.log.info("Recording started: system audio (%s) rate=%s Hz",
                      name, self._system_rate)

    def stop(self):
        """Stop and return the buffer (float32, 16 kHz mono) or None."""
        self._stop_evt.set()
        if self._system_rec is not None:
            # Closing the loopback recorder unblocks a pending record().
            with contextlib.suppress(Exception):
                self._system_rec.__exit__(None, None, None)
            self._system_rec = None
        if self._system_thread:
            self._system_thread.join(timeout=2.0)
        if self._monitor:
            self._monitor.join(timeout=2.0)
        if self._stream:
            # PortAudio raises a mix of errors on a dead stream
            with contextlib.suppress(Exception):
                self._stream.stop()
                self._stream.close()
            self._stream = None
        with self._lock:
            if not self._frames and not self._take_frames:
                return None
            frames = (self._take_frames + self._frames
                      if self._take_frames else self._frames)
            buf = np.concatenate(frames)
            self._frames = []
            self._take_frames = []
        dur = len(buf) / SAMPLE_RATE
        self.log.info(
            "Recording stopped: %.1f s, queue overruns: %d", dur, self._overruns
        )
        if self.live_state is not None:
            self.live_state["level"] = 0.0
        return buf

    def had_speech(self):
        return self._has_speech

    # -- internals ---------------------------------------------------------
    def _callback(self, indata, frames, time_info, status):
        if status:
            self._overruns += 1
        try:
            # A stereo attempt is averaged down: the VAD and the model are mono.
            if indata.ndim > 1 and indata.shape[1] > 1:
                mono = indata.mean(axis=1)
            elif indata.ndim > 1:
                mono = indata[:, 0]
            else:
                mono = indata
            self._push(mono)
        except Exception:  # noqa: BLE001 - the audio thread must never die
            self._overruns += 1

    def _push(self, mono):
        """Resample to 16 kHz, then cut exact FRAME chunks into the queue."""
        if self._resampler is not None:
            mono = self._resampler.process(mono)
        if not len(mono):
            return
        # The queue and the VAD take exact 16 kHz frames; the resampler's
        # output length wobbles around 512, so the remainder waits here.
        if len(self._pending):
            self._pending = np.concatenate((self._pending, mono))
        else:
            self._pending = np.asarray(mono, dtype=np.float32)
        while len(self._pending) >= FRAME:
            chunk = self._pending[:FRAME].copy()
            self._pending = self._pending[FRAME:]
            try:
                self._q.put_nowait(chunk)
            except queue.Full:
                self._overruns += 1

    def _system_loop(self):
        """Read loopback blocks and feed the same frame queue."""
        while not self._stop_evt.is_set():
            try:
                data = self._system_rec.record(numframes=self._system_block)
                arr = np.asarray(data, dtype=np.float32)
                mono = arr.mean(axis=1) if arr.ndim > 1 else arr
                self._push(mono)
            except Exception as e:  # noqa: BLE001 - a dead loopback must not hang the take
                if self._stop_evt.is_set():
                    return
                self.log.info("System audio stopped: %s", e)
                self._auto_stop("error")
                return

    def _monitor_loop(self):
        while not self._stop_evt.is_set():
            try:
                frame = self._q.get(timeout=0.1)
            except queue.Empty:
                self._check_timeouts()
                continue
            self._ingest(frame.reshape(-1))

    def _ingest(self, data):
        """One monitor pass: buffer the frame, run VAD, cut a chunk, check limits."""
        with self._lock:
            self._frames.append(data.copy())
            if self._source == "system":
                # Live subtitles read only the tail; a long film must not
                # grow the buffer for hours. Microphone takes keep their
                # frames — the continuous chunk path owns that buffer.
                limit = int(SYSTEM_BUFFER_S * SAMPLE_RATE) // FRAME
                if len(self._frames) > limit:
                    del self._frames[:len(self._frames) - limit]
        self.level = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
        prob = self._vad_prob(data)
        self.vad_p = prob
        if self.live_state is not None:
            self.live_state["level"] = self.level
            self.live_state["vad"] = self.vad_p
        voiced = prob >= VOICE_THRESHOLD
        if voiced:
            self._t_last_voice = time.monotonic()
            self._has_speech = True
        self._track_chunk(len(data), voiced)
        self._maybe_partial(float(self.cfg.get("partial_every_s", 1.5)))
        self._check_timeouts()

    def chunk_ready(self):
        """The receiver has room again: stop deferring chunk cuts."""
        self._chunk_deferred = False

    def _track_chunk(self, samples, voiced):
        """Cut the live take into bounded chunks (continuous mode only)."""
        if not self._continuous:
            return
        self._chunk_samples += samples
        if voiced:
            self._voice_end = self._chunk_samples
            self._quiet_samples = 0
            self._voiced_samples += samples
        else:
            self._quiet_samples += samples
        pause = int(CONTINUOUS_PAUSE_MS * SAMPLE_RATE / 1000)
        if (self._voiced_samples >= int(CONTINUOUS_MIN_S * SAMPLE_RATE)
                and self._quiet_samples >= pause):
            # A pause is the natural seam: the chunk ends exactly on the last
            # voiced frame, so no word is cut and no overlap is needed.
            self._flush_chunk(self._voice_end, 0)
            return
        if self._chunk_samples >= int(CONTINUOUS_CHUNK_S * SAMPLE_RATE):
            # The speaker runs on: cut at the cap and carry the tail into the
            # next chunk, so a word split here is complete in the next pass.
            self._flush_chunk(self._chunk_samples,
                              int(CONTINUOUS_OVERLAP_S * SAMPLE_RATE))
            return
        if (self._chunk_deferred
                and self._chunk_samples >= int(CONTINUOUS_HARD_MAX_S * SAMPLE_RATE)):
            # Recognition is far behind; keeping the audio would grow without
            # bound. Drop this piece and say so — never silently.
            self.log.warning(
                "Continuous chunk dropped: %.0f s buffered while recognition "
                "is behind", self._chunk_samples / SAMPLE_RATE)
            self._drop_buffer()

    def _flush_chunk(self, end, keep):
        """Hand one chunk to the receiver; trim the buffer only when it took it."""
        if self._chunk_deferred:
            return
        with self._lock:
            frames = self._frames
            total = len(frames) * FRAME
            count = min(end, total) // FRAME
            if count <= 0:
                return
            keep_count = min(keep // FRAME, count)
            chunk_frames = list(frames[:count])
            rest = list(frames[count:])
            overlap = chunk_frames[count - keep_count:] if keep_count else []
            trimmed = overlap + rest
        chunk = np.concatenate(chunk_frames)
        accepted = True
        if self.on_chunk is not None:
            try:
                accepted = self.on_chunk(chunk) is not False
            except Exception as e:  # noqa: BLE001 - the take must keep going
                self.log.warning("Chunk handoff failed: %s", e)
                accepted = False
        if not accepted:
            self._chunk_deferred = True
            return
        with self._lock:
            self._frames = trimmed
            if self._keep_take:
                self._take_frames.extend(
                    chunk_frames[:count - keep_count])
        self._chunk_samples = len(trimmed) * FRAME
        self._voice_end = 0
        self._quiet_samples = self._chunk_samples
        self._voiced_samples = 0

    def _drop_buffer(self):
        with self._lock:
            self._frames = []
        self._chunk_samples = 0
        self._voice_end = 0
        self._quiet_samples = 0
        self._voiced_samples = 0

    def _maybe_partial(self, partial_every_s):
        """Fire a partial transcribe every partial_every_s of live speech."""
        if self.on_partial is None or not self._has_speech:
            return
        every = float(partial_every_s)
        if self._source == "system":
            # A film's recognition is heavier than dictation's and the plate
            # is read for seconds: update it less often, never faster than
            # SYSTEM_PARTIAL_EVERY_S.
            every = max(every, SYSTEM_PARTIAL_EVERY_S)
        now = time.monotonic()
        if now - self._t_last_partial < every:
            return
        with self._lock:
            if len(self._frames) < SAMPLE_RATE // FRAME:  # < 1 s of audio
                return
            snapshot = partial_snapshot(self._frames)
        self._t_last_partial = now
        threading.Thread(target=self.on_partial, args=(snapshot,), daemon=True).start()

    def _check_timeouts(self):
        if self._source == "system":
            # A film may have long quiet passages and no natural end: the
            # take closes by the hotkey (or a loopback error), never by a
            # timer — no silence auto-stop and no max_record_s cut.
            return
        now = time.monotonic()
        elapsed = now - self._t_start
        if not self._continuous and elapsed >= self.cfg["max_record_s"]:
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
        self.log.info("Auto-stop: %s", reason)
        self._stop_evt.set()
        threading.Thread(target=self.on_auto_stop, args=(reason,), daemon=True).start()

    def _vad_prob(self, window):
        try:
            probs = self.vad(window, num_samples=FRAME)
            return float(probs[-1])
        except Exception as e:  # noqa: BLE001 - optional VAD: any failure falls back to no speech
            self.log.debug("VAD skipped: %s", e)
            return 0.0
