"""ASR engine: one WhisperModel per service lifetime, dictation and files.

Two entrances, one engine: ``transcribe`` takes 16 kHz float32 audio from the
microphone, ``transcribe_file`` takes any path PyAV can decode. The decoder is
our own: faster-whisper 1.2.1 passes ``metadata_errors`` to ``av.open``, which
av 19.0.2 rejects, so a path handed to ``model.transcribe`` raises TypeError.
"""
import contextlib
import gc
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
from config import MODELS_DIR, bootstrap_cuda_dlls
from transcript import Segment, Word

try:  # faster-whisper >= 1.1 ships the batched wrapper; older builds do not
    from faster_whisper import BatchedInferencePipeline
except Exception:  # noqa: BLE001, pragma: no cover - an old faster-whisper build
    BatchedInferencePipeline = None

VAD_PARAMETERS = {"min_silence_duration_ms": 1000, "speech_pad_ms": 400}
NO_SPEECH_THRESHOLD = 0.6
TARGET_RATE = 16000
# File-mode preprocessing block: the VAD pass and feature extraction run on one
# block at a time, so the model lock is never held for a whole file. A
# dictation that starts mid-file waits at most one block (plus one block of
# decode), never the rest of the file.
FILE_CHUNK_S = 600
# Batched file mode: files at or above this duration go through
# BatchedInferencePipeline (several 30 s windows per model pass), so the wall
# clock drops on long jobs. Short files pay the batching overhead for nothing
# and keep the sequential path; dictation never batches.
BATCH_MIN_S = 60
BATCH_SIZE_DEFAULT = 8
BATCH_SIZE_MAX = 16
AV_TIME_BASE = 1_000_000   # container.duration is in microseconds


class AsrCancelled(Exception):
    """The caller asked the file transcription to stop between segments."""


class FileTooLong(ValueError):
    """The file is longer than ``file_max_s``; refused before a full decode."""

    def __init__(self, duration: float, limit: float) -> None:
        self.duration = float(duration)
        self.limit = float(limit)
        super().__init__(
            f"file is {self.duration:.0f} s, longer than file_max_s="
            f"{self.limit:.0f} s")


class ModelNotInstalled(ValueError):
    """The requested model is not installed; the engine is never switched."""


def check_model(requested: str, cfg_model: str) -> str:
    """Validate a job's ``model`` field against the one installed engine.

    The file mode does not switch models: an empty value keeps the config
    model, and a non-empty one is accepted only as an alias of the installed
    model (its name, its repo id, or the OpenAI-standard ``whisper-1``). No
    download is attempted from here.
    """
    name = str(requested or "").strip()
    if not name:
        return ""
    current = str(cfg_model or "").strip()
    allowed = {"whisper-1"}
    if current:
        allowed.add(current)
        allowed.add(_repo_id(current))
    if name in allowed:
        return name
    raise ModelNotInstalled(
        f"model {name} is not installed; this build uses {current}")


def _known_duration(container: Any, stream: Any) -> float | None:
    """The container's declared duration in seconds, when it has one."""
    stream_duration = getattr(stream, "duration", None)
    time_base = getattr(stream, "time_base", None)
    if stream_duration and time_base:
        return float(stream_duration * time_base)
    container_duration = getattr(container, "duration", None)
    if container_duration:
        return float(container_duration) / AV_TIME_BASE
    return None


def _mono(frame) -> np.ndarray:
    """One PyAV audio frame as a flat float32 array."""
    data = frame.to_ndarray()
    if data.ndim > 1:
        data = data.reshape(-1)
    return data.astype(np.float32, copy=False)


def decode_audio_file(path: str, log: logging.Logger | None = None,
                      max_s: float | None = None) -> tuple[np.ndarray, float]:
    """Decode a media file to 16 kHz mono float32 with PyAV alone.

    No ffmpeg binary and no ``faster_whisper.audio.decode_audio``: the latter
    is what breaks on av 19.0.2. Returns the samples and the duration in
    seconds; a file without an audio stream is refused, not guessed.

    ``max_s`` enforces the duration cap before memory is spent: a declared
    container/stream duration is compared up front and the decode loop stops
    as soon as an unknown duration crosses the cap. The message names
    ``file_max_s`` and the limit.
    """
    import av

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    limit = float(max_s or 0.0)
    chunks: list[np.ndarray] = []
    total = 0
    with av.open(path) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio stream in the file")
        known = _known_duration(container, stream)
        if limit and known is not None and known > limit:
            raise FileTooLong(known, limit)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=TARGET_RATE)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunk = _mono(out)
                total += chunk.shape[0]
                if limit and total / float(TARGET_RATE) > limit:
                    raise FileTooLong(total / float(TARGET_RATE), limit)
                chunks.append(chunk)
        for out in resampler.resample(None):
            chunk = _mono(out)
            total += chunk.shape[0]
            if limit and total / float(TARGET_RATE) > limit:
                raise FileTooLong(total / float(TARGET_RATE), limit)
            chunks.append(chunk)
    if not chunks:
        return np.zeros((0,), dtype=np.float32), 0.0
    audio = np.concatenate(chunks)
    duration = audio.shape[0] / float(TARGET_RATE)
    if log is not None:
        log.debug("Decoded %s: %.3f s, %d bytes", path, duration, audio.nbytes)
    return audio, duration

# Offline-first runtime: a cached model loads with no network; the network is
# used only for the first download of a model that is nowhere on disk.

# Canonical CT2 conversions that live outside Systran (Systran has no turbo
# repo; mobiuslabsgmbh/faster-whisper-large-v3-turbo redirects to dropbox-dash).
REPO_OVERRIDES = {
    "large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
}

# Dictation presets per pinned language: punctuation + capitalization hints.
# User-specific terms go to config.json "hotwords", never into the repo.
PROMPT_PRESETS = {
    "ru": "Диктовка с пунктуацией и заглавными буквами.",
    "en": "Dictation with punctuation and capitalization.",
    "uk": "Диктування з пунктуацією та великими буквами.",
    "de": "Diktat mit Zeichensetzung und Großschreibung.",
    "fr": "Dictée avec ponctuation et majuscules.",
    "es": "Dictado con puntuación y mayúsculas.",
}


def _repo_id(name):
    """Hugging Face repo id for a short model name."""
    if "/" in name:
        return name
    return REPO_OVERRIDES.get(name, f"Systran/faster-whisper-{name}")


def _resolve_model(name, log):
    """Path or repo id to load: models/<name> inside the service folder
    first, the shared HF cache second, a fresh download into models/<name>
    last — the installation stays self-contained."""
    import os

    local = os.path.join(MODELS_DIR, name)
    if os.path.exists(os.path.join(local, "model.bin")):
        return local, True  # local dir: no hub access at all
    repo = _repo_id(name)
    with contextlib.suppress(Exception):
        from huggingface_hub import snapshot_download
        snapshot_download(repo, local_files_only=True)
        return name, True   # already in the shared HF cache
    os.makedirs(MODELS_DIR, exist_ok=True)
    log.info("Downloading model %s to %s (first-time install)", name, local)
    from huggingface_hub import snapshot_download
    snapshot_download(repo, local_dir=local)
    return local, False


def _make_model(name, device, compute_type, log):
    from faster_whisper import WhisperModel

    source, offline = _resolve_model(name, log)
    if not offline:
        return WhisperModel(source, device=device, compute_type=compute_type,
                            local_files_only=False)
    try:
        return WhisperModel(source, device=device, compute_type=compute_type,
                            local_files_only=True)
    except Exception:  # noqa: BLE001 - the offline model load can fail in third-party code; retry online
        return WhisperModel(source, device=device, compute_type=compute_type,
                            local_files_only=False)


def _prompt(cfg):
    """Initial prompt: preset for a pinned language, hotwords always."""
    parts = []
    lang = cfg.get("language")
    if lang and lang != "auto":
        preset = PROMPT_PRESETS.get(lang)
        if preset:
            parts.append(preset)
    import dictionary
    hotwords = dictionary.merge_hotwords(cfg.get("hotwords"),
                                         cfg.get("dictionary"))
    if hotwords:
        parts.append(hotwords)
    return " ".join(parts) or None


class ASR:
    def __init__(self, cfg, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.model = None
        self._lock = threading.Lock()
        self._batched = None        # one BatchedInferencePipeline per model
        self._batch_error = None    # why the batched path is unavailable

    def load(self):
        """Load once; keep in memory for the whole service lifetime."""
        bootstrap_cuda_dlls()

        with self._lock:
            self._batched = None
            self._batch_error = None
            t0 = time.time()
            self.log.info(
                "Loading model %s: device=%s compute_type=%s",
                self.cfg["model"], self.cfg["device"], self.cfg["compute_type"],
            )
            self.model = _make_model(self.cfg["model"], self.cfg["device"],
                                     self.cfg["compute_type"], self.log)
            self.log.info("Model loaded in %.1f s", time.time() - t0)

    def _language(self, language: str | None = None) -> str:
        """The pinned language: the job's, else the config's, else English.

        ``auto`` stays out of the contract; a legacy hand-edited value maps to
        English like the dictation path always did.
        """
        lang = str(language or self.cfg.get("language") or "").strip()
        if not lang or lang == "auto":
            return "en"
        return lang

    @property
    def model_name(self) -> str:
        """The model actually loaded; metadata reports this, not a request."""
        return str(self.cfg.get("model") or "")

    def _transcribe_impl(self, audio: np.ndarray, language: str, *,
                         word_timestamps: bool = False,
                         initial_prompt: str | None = None,
                         temperature: float | None = None):
        """One call to the model with the shared VAD, prompt and guards.

        Dictation and the file mode both enter here, so the anti-hallucination
        settings and the prompt can never drift apart. An explicit prompt or
        temperature from a file job overrides the configured default; omitted
        values keep today's behavior.
        """
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = float(temperature)
        return self.model.transcribe(
            audio,
            language=language,
            beam_size=5,
            initial_prompt=(initial_prompt if initial_prompt is not None
                            else _prompt(self.cfg)),
            condition_on_previous_text=False,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            vad_filter=True,
            vad_parameters=dict(VAD_PARAMETERS),
            word_timestamps=word_timestamps,
            **options,
        )

    def _batch_size(self, value: int | None = None) -> int:
        """The batch size for a batched pass, clamped to 1..``BATCH_SIZE_MAX``.

        A missing or non-numeric setting falls back to ``BATCH_SIZE_DEFAULT``
        instead of failing a job: this is a performance knob, not an input.
        """
        raw = self.cfg.get("file_batch_size", BATCH_SIZE_DEFAULT) if value is None else value
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return BATCH_SIZE_DEFAULT
        return max(1, min(BATCH_SIZE_MAX, size))

    def _get_batched_pipeline(self):
        """One ``BatchedInferencePipeline`` on the loaded model; None if absent.

        The wrapper shares the already loaded model, so a second model is
        never created. Creation happens under the lock (a model switch must
        never leave a pipeline on a deleted model); the batched pass itself
        runs under the same lock, so this is called before the block loop.
        """
        if BatchedInferencePipeline is None:
            self._batch_error = "BatchedInferencePipeline is not available"
            self.log.warning("Batched pipeline unavailable: %s; using the sequential path", self._batch_error)
            return None
        with self._lock:
            if self.model is None:
                self._batch_error = "no model is loaded"
                self.log.warning("Batched pipeline unavailable: %s; using the sequential path", self._batch_error)
                return None
            if self._batched is None:
                try:
                    self._batched = BatchedInferencePipeline(self.model)
                except Exception as exc:  # noqa: BLE001 - any failure means sequential
                    self._batch_error = str(exc) or type(exc).__name__
                    self.log.warning("Batched pipeline unavailable: %s; using the sequential path", exc)
                    return None
                self._batch_error = None
            return self._batched

    def _transcribe_batched(self, audio: np.ndarray, language: str, *,
                            word_timestamps: bool,
                            initial_prompt: str | None,
                            temperature: float | None,
                            batch_size: int):
        """One batched pass over one ``FILE_CHUNK_S`` block.

        The options are the same VAD, prompt and anti-hallucination guards as
        the sequential path; only the window scheduling differs.
        """
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = float(temperature)
        return self._batched.transcribe(
            audio,
            language=language,
            beam_size=5,
            initial_prompt=(initial_prompt if initial_prompt is not None
                            else _prompt(self.cfg)),
            condition_on_previous_text=False,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            vad_filter=True,
            vad_parameters=dict(VAD_PARAMETERS),
            word_timestamps=word_timestamps,
            # The wrapper defaults to True, which is not the sequential path's
            # behavior: repeated speech inside one window then loses whole
            # repeats (measured CER ~27% on the 660 s fixture). With
            # timestamps on, the transcript matches the sequential one.
            without_timestamps=False,
            batch_size=batch_size,
            **options,
        )

    def transcribe(self, audio: np.ndarray, verbose: bool = True) -> str:
        """Transcribe 16 kHz float32 mono audio; returns stripped text.

        The language is pinned exactly (config) — never guessed. A legacy
        hand-edited "auto" value maps to "en".
        """
        with self._lock:
            t0 = time.time()
            lang = self._language()
            segments, info = self._transcribe_impl(audio, lang)
            text = "".join(s.text for s in segments).strip()
            if verbose:
                self.log.info(
                    "Transcription: audio %.1f s, recognition %.2f s, lang=%s",
                    audio.shape[0] / 16000.0, time.time() - t0, info.language,
                )
            return text

    def _pause(self, should_pause: Callable[[], bool] | None,
               should_stop: Callable[[], bool] | None) -> None:
        """Wait while the caller asks to pause; a stop wins over the pause."""
        while should_pause is not None and should_pause():
            time.sleep(0.1)
            if should_stop is not None and should_stop():
                raise AsrCancelled()

    def transcribe_array(self, audio: np.ndarray, duration: float, *,
                         language: str | None = None,
                         word_timestamps: bool = False,
                         initial_prompt: str | None = None,
                         temperature: float | None = None,
                         on_progress: Callable[[float], None] | None = None,
                         on_duration: Callable[[float], None] | None = None,
                         should_pause: Callable[[], bool] | None = None,
                         should_stop: Callable[[], bool] | None = None,
                         batched: bool = False,
                         batch_size: int | None = None,
                         ) -> Iterator[Segment]:
        """Yield segments from decoded audio; pause and stop between them.

        The audio is processed in ``FILE_CHUNK_S`` blocks: the model lock is
        taken per block and per segment, never for the whole file. A dictation
        that starts mid-file waits for the block in flight and then has the
        engine to itself; the worst case is one block, not the file.

        ``batched=True`` runs each block through ``BatchedInferencePipeline``;
        a failure falls back to the sequential path for the rest of the job,
        with ``last_end`` keeping the transcript free of duplicates.
        """
        lang = self._language(language)
        total = max(0.0, float(duration or (audio.shape[0] / float(TARGET_RATE))))
        if on_duration is not None:
            on_duration(total)
        samples = int(audio.shape[0])
        if samples <= 0:
            self.log.info("File transcription: empty audio")
            if on_progress is not None:
                on_progress(100.0)
            return
        block_samples = max(1, int(FILE_CHUNK_S * TARGET_RATE))
        blocks = (samples + block_samples - 1) // block_samples
        size = self._batch_size(batch_size)
        use_batched = bool(batched)
        if use_batched and self._get_batched_pipeline() is None:
            use_batched = False
        self.log.info("File transcription started: %.1f s, lang=%s, blocks=%d, "
                      "batched=%s", total, lang, blocks, use_batched)

        last_end = 0.0       # end of the last segment handed to the caller
        last_progress = 0.0
        fallback_used = False

        def report(value: float) -> None:
            nonlocal last_progress
            if on_progress is None:
                return
            value = min(99.0, max(0.0, value))
            if value < last_progress:
                value = last_progress
            last_progress = value
            on_progress(value)

        def emit(segments, block_start: float, dedupe: bool) -> Iterator[Segment]:
            """Yield one block's segments, optionally skipping what was seen."""
            nonlocal last_end
            while True:
                if should_stop is not None and should_stop():
                    raise AsrCancelled()
                self._pause(should_pause, should_stop)
                with self._lock:
                    try:
                        seg = next(segments)
                    except StopIteration:
                        return
                text = (seg.text or "").strip()
                start = block_start + float(seg.start)
                end = block_start + float(seg.end)
                if dedupe:
                    if end <= last_end:
                        continue
                    start = max(start, last_end)
                words = None
                if seg.words:
                    words = [Word(start=block_start + float(w.start),
                                  end=block_start + float(w.end),
                                  text=w.word) for w in seg.words]
                if text:
                    if end < start:
                        end = start
                    last_end = max(last_end, end)
                    yield Segment(start=start, end=end, text=text, words=words)
                if total > 0:
                    report(end / total * 100.0)

        for index in range(blocks):
            if should_stop is not None and should_stop():
                raise AsrCancelled()
            self._pause(should_pause, should_stop)
            offset = index * block_samples
            block = audio[offset:offset + block_samples]
            block_start = offset / float(TARGET_RATE)
            if use_batched:
                try:
                    with self._lock:
                        segments, info = self._transcribe_batched(
                            block, lang, word_timestamps=word_timestamps,
                            initial_prompt=initial_prompt,
                            temperature=temperature, batch_size=size)
                    if index == 0:
                        self.log.info("File transcription engine language: %s",
                                      info.language)
                    yield from emit(segments, block_start, dedupe=False)
                    continue
                except AsrCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - a job always finishes
                    self.log.warning("Batched pipeline failed: %s; falling back to the sequential path", exc)
                    use_batched = False
                    fallback_used = True
            with self._lock:
                segments, info = self._transcribe_impl(
                    block, lang, word_timestamps=word_timestamps,
                    initial_prompt=initial_prompt, temperature=temperature)
            if index == 0:
                self.log.info("File transcription engine language: %s",
                              info.language)
            yield from emit(segments, block_start, dedupe=fallback_used)
        if on_progress is not None:
            on_progress(100.0)

    def transcribe_file(self, path: str, *, language: str | None = None,
                        word_timestamps: bool = False,
                        initial_prompt: str | None = None,
                        temperature: float | None = None,
                        max_s: float | None = None,
                        on_progress: Callable[[float], None] | None = None,
                        on_duration: Callable[[float], None] | None = None,
                        should_pause: Callable[[], bool] | None = None,
                        should_stop: Callable[[], bool] | None = None,
                        batched: bool | None = None,
                        batch_size: int | None = None,
                        ) -> Iterator[Segment]:
        """Transcribe a media file: own PyAV decode, then the same engine.

        Decoding happens on the first iteration, so a caller that only builds
        the iterator pays nothing; the cancellation check runs before it and
        ``max_s`` refuses an over-long file before memory is spent.

        ``batched=None`` decides by config and duration: long files use the
        batched pipeline when ``file_batched`` is on, short ones never do.
        The job queue passes nothing; the decision is the config's.
        """
        if should_stop is not None and should_stop():
            raise AsrCancelled()
        t0 = time.time()
        audio, duration = decode_audio_file(path, self.log, max_s=max_s)
        self.log.info("Decoded file: %.1f s in %.1f s, %d bytes",
                      duration, time.time() - t0, audio.nbytes)
        if batched is None:
            use_batched = (bool(self.cfg.get("file_batched", True))
                           and duration >= BATCH_MIN_S)
        else:
            use_batched = bool(batched)
        yield from self.transcribe_array(
            audio, duration, language=language, word_timestamps=word_timestamps,
            initial_prompt=initial_prompt, temperature=temperature,
            on_progress=on_progress, on_duration=on_duration,
            should_pause=should_pause, should_stop=should_stop,
            batched=use_batched, batch_size=self._batch_size(batch_size),
        )

    def reload_model(self, name) -> bool:
        """Swap the model in place (tray menu); returns success."""
        with self._lock:
            try:
                old = self.model
                self.model = None
                self._batched = None
                self._batch_error = None
                del old
                gc.collect()
                self.cfg["model"] = name
                self.log.info("Switching model to %s", name)

                t0 = time.time()
                self.model = _make_model(name, self.cfg["device"],
                                         self.cfg["compute_type"], self.log)
                self.log.info("Model %s loaded in %.1f s", name, time.time() - t0)
                return True
            except Exception as e:
                self.log.exception("Model switch failed: %s", e)
                return False
