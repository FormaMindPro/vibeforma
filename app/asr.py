"""ASR engine: one WhisperModel per service lifetime, dictation and files.

Two entrances, one engine: ``transcribe`` takes 16 kHz float32 audio from the
microphone, ``transcribe_file`` takes any path PyAV can decode. The decoder is
our own: faster-whisper 1.2.1 passes ``metadata_errors`` to ``av.open``, which
av 19.0.2 rejects, so a path handed to ``model.transcribe`` raises TypeError.
"""
import contextlib
import gc
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import hallucination
import numpy as np
from config import (
    MODELS_DIR,
    QUALITY_PRESET_DEFAULT,
    QUALITY_PRESETS,
    bootstrap_cuda_dlls,
)
from transcript import Segment, Word

try:  # faster-whisper >= 1.1 ships the batched wrapper; older builds do not
    from faster_whisper import BatchedInferencePipeline
except Exception:  # noqa: BLE001, pragma: no cover - an old faster-whisper build
    BatchedInferencePipeline = None

VAD_PARAMETERS = {"min_silence_duration_ms": 1000, "speech_pad_ms": 400}
NO_SPEECH_THRESHOLD = 0.6
# The sequential call's other two per-segment thresholds, applied to the
# batched path's segments too (faster-whisper 1.2.1 applies none there, B5).
# ``log_prob`` is the library's own default. ``compression_ratio`` keeps the
# calibrated 1.35 only for the language it was measured on; every other
# language keeps the library default, which is what the sequential call uses
# there (clean Russian F0 speech reads 1.476, so 1.35 would re-decode every
# window and lose the batching's point).
LOG_PROB_THRESHOLD = -1.0
COMPRESSION_RATIO_DEFAULT = 2.4
# Dictation presets: a live partial is greedy and single-temperature; the
# final dictation pass defaults to beam 5 and a short fallback ladder (a
# quality preset may widen or narrow it). File mode does not use these
# values and keeps faster-whisper's full ladder.
DICTATION_BEAM_SIZE = 5
DICTATION_TEMPERATURES = (0.0, 0.2)
PARTIAL_BEAM_SIZE = 1
PARTIAL_TEMPERATURE = 0.0
# The draft's wider pass (card-247): the same take, the same model, a wider
# beam and one temperature. No fallback ladder: the retry is the best search
# at the confident setting, not another walk down the ten temperatures the
# final dictation pass uses.
RETRANSCRIBE_BEAM_SIZE = 10
RETRANSCRIBE_TEMPERATURE = 0.0
# Quality presets of the final dictation pass (card-265): normal is the
# shipped behavior, accurate reuses the draft retry's wider search, fast
# reuses the live partial's greedy search (no fallback ladder). The preset is
# the only source of this pass's beam and temperature: the settings window
# exposes no raw temperature, and nothing else overrides these values here.
QUALITY_PRESET_PARAMS = {
    "normal": (DICTATION_BEAM_SIZE, DICTATION_TEMPERATURES),
    "accurate": (RETRANSCRIBE_BEAM_SIZE, RETRANSCRIBE_TEMPERATURE),
    "fast": (PARTIAL_BEAM_SIZE, PARTIAL_TEMPERATURE),
}
# Language hint (F12): recognition always uses the pinned language and never
# switches (card-118); the hint only tells the person the take sounded like
# another one. It needs both readings at once: the pinned language below the
# floor and the best other language at or above the lead. A confident take
# never hints, and a spread-out distribution never does either.
LANGUAGE_HINT_FLOOR = 0.5
LANGUAGE_HINT_LEAD = 0.5
TARGET_RATE = 16000
# File-mode preprocessing block: the VAD pass and feature extraction run on one
# block at a time, so the model lock is never held for a whole file. A
# dictation that starts mid-file waits at most one block (plus one block of
# decode), never the rest of the file.
FILE_CHUNK_S = 600
# The seam between blocks: every block is a fresh engine call, so the model
# starts cold at 600 s, 1200 s, ... and a name or a style decided in the
# previous block drifts. The last FILE_CONTEXT_TAIL_CHARS characters of the
# block just decoded ride as the next block's initial prompt. The bound keeps
# the prompt the same size for every block (the tail replaces, never
# accumulates), and it sits at the end of the prompt, where
# faster_whisper's get_prompt keeps it (it truncates from the start at about
# 225 tokens). Zero turns the carry off and restores the block-local prompt.
FILE_CONTEXT_TAIL_CHARS = 200
# Batched file mode: files at or above this duration go through
# BatchedInferencePipeline (several 30 s windows per model pass), so the wall
# clock drops on long jobs. Short files pay the batching overhead for nothing
# and keep the sequential path; dictation never batches.
BATCH_MIN_S = 60
BATCH_SIZE_DEFAULT = 8
BATCH_SIZE_MAX = 16
AV_TIME_BASE = 1_000_000   # container.duration is in microseconds


def parse_clip_timestamps(value: str | list[float] | None,
                          duration: float | None = None,
                          ) -> list[tuple[float, float]]:
    """A clip range as start,end pairs; an odd last end runs to the file end.

    The value is the OpenAI/faster-whisper shape: a comma-separated string or
    a list of numbers, "start,end,start,end,...". An empty value means the
    whole file ([]). A range is clamped to ``duration`` when it is known, so a
    clip never reaches past the file; a pair that becomes empty after the
    clamp is dropped. Anything unparseable or reversed is a ValueError.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw: list[Any] = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raise ValueError("clip_timestamps must be a string or a list of numbers")
    if not raw or (len(raw) == 1 and raw[0] == ""):
        return []
    numbers: list[float] = []
    for item in raw:
        if isinstance(item, str) and not item:
            raise ValueError("clip_timestamps: empty value between commas")
        try:
            number = float(item)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"clip_timestamps: not a number: {item!r}") from error
        if not math.isfinite(number):
            raise ValueError("clip_timestamps: not a finite number")
        numbers.append(max(0.0, number))
    if len(numbers) % 2:
        numbers.append(math.inf)
    pairs: list[tuple[float, float]] = []
    for start, end in zip(numbers[::2], numbers[1::2], strict=True):
        if duration is not None:
            limit = max(0.0, float(duration))
            if start >= limit:
                continue
            end = min(end, limit)
        if end <= start:
            raise ValueError("clip_timestamps: the end must be after the start")
        pairs.append((start, end))
    return pairs


def format_clip_timestamps(pairs: list[tuple[float, float]]) -> str:
    """The normalized comma form a job reports back: "start,end,..."."""
    parts: list[str] = []
    for start, end in pairs:
        parts.append(f"{start:g}")
        if not math.isinf(end):
            parts.append(f"{end:g}")
    return ",".join(parts)


def language_hint(probs: list[tuple[str, float]] | None,
                  pinned: str) -> dict[str, Any] | None:
    """The other language an unsure take reads as, for the HUD hint.

    ``probs`` is the engine's full distribution over languages (the reading
    ``WhisperModel.detect_language`` returns); ``pinned`` is the recognition
    language the person chose, which never changes. The hint fires only when
    both readings agree in opposite directions: the pinned language is below
    ``LANGUAGE_HINT_FLOOR`` and another language is at or above
    ``LANGUAGE_HINT_LEAD``. A pinned language missing from the distribution
    has no reading of its own, so no hint is made from it.
    """
    if not probs:
        return None
    readings = {str(code): float(prob) for code, prob in probs}
    pinned_prob = readings.get(pinned)
    if pinned_prob is None or pinned_prob >= LANGUAGE_HINT_FLOOR:
        return None
    other, other_prob = max(readings.items(), key=lambda item: item[1])
    if other == pinned or other_prob < LANGUAGE_HINT_LEAD:
        return None
    return {"pinned": pinned, "pinned_prob": pinned_prob,
            "other": other, "other_prob": other_prob}


def file_context_tail(text: str, limit: int | None = None) -> str:
    """The last ``limit`` characters of a block's text, cut at a word start.

    The tail is the seam hint: the words a block ended on, so the next block
    begins with the same names and style. It is one bounded string, never the
    block's whole text, and it never accumulates. ``limit`` defaults to
    ``FILE_CONTEXT_TAIL_CHARS`` at call time, so zero (or a negative value)
    turns the carry off.
    """
    body = " ".join(str(text or "").split())
    bound = FILE_CONTEXT_TAIL_CHARS if limit is None else int(limit)
    if bound <= 0 or not body:
        return ""
    if len(body) <= bound:
        return body
    tail = body[-bound:]
    space = tail.find(" ")
    if space != -1:
        tail = tail[space + 1:]
    return tail.strip()


def _file_prompt(base: str | None, tail: str) -> str | None:
    """A block's initial prompt: the base hint, then the previous block's tail.

    The tail goes last on purpose — faster-whisper truncates the prompt from
    the start, so the words nearest the audio are the ones that survive — and
    the join is at most two parts, so the prompt never grows with the number
    of blocks. An empty tail returns the base unchanged.
    """
    if not tail:
        return base
    if not base:
        return tail
    return f"{base} {tail}"


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


class ModelPathInvalid(ValueError):
    """A configured model folder is not a usable CTranslate2 model."""


def check_model(requested: str, cfg_model: str) -> str:
    """Validate a job's ``model`` field against the one installed engine.

    The file mode does not switch models: an empty value keeps the config
    model, and a non-empty one is accepted only as an alias of the installed
    model (its name, its repo id, its folder path, or the OpenAI-standard
    ``whisper-1``). No download is attempted from here.
    """
    name = str(requested or "").strip()
    if not name:
        return ""
    current = str(cfg_model or "").strip()
    allowed = {"whisper-1"}
    if current:
        allowed.add(current)
        if _model_path(current) is None:
            allowed.add(_repo_id(current))
    if name in allowed:
        return name
    wanted = _model_path(name)
    have = _model_path(current) if current else None
    if (wanted is not None and have is not None
            and os.path.normcase(wanted) == os.path.normcase(have)):
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
# One entry per language of the menu (config.LANG_NAMES, no "auto"), each
# written in that language; tests/test_prompt_presets.py fails on a missing
# language by name. User-specific terms go to config.json "hotwords", never
# into the repo.
PROMPT_PRESETS = {
    "en": "Dictation with punctuation and capitalization.",
    "ru": "Диктовка с пунктуацией и заглавными буквами.",
    "uk": "Диктування з пунктуацією та великими буквами.",
    "de": "Diktat mit Zeichensetzung und Großschreibung.",
    "fr": "Dictée avec ponctuation et majuscules.",
    "es": "Dictado con puntuación y mayúsculas.",
    "it": "Dettatura con punteggiatura e maiuscole.",
    "pt": "Ditado com pontuação e maiúsculas.",
    "nl": "Dicteren met leestekens en hoofdletters.",
    "pl": "Dyktowanie z interpunkcją i wielkimi literami.",
    "cs": "Diktování s interpunkcí a velkými písmeny.",
    "sv": "Diktering med skiljetecken och stora bokstäver.",
    "da": "Diktering med tegnsætning og store bogstaver.",
    "fi": "Sanelu välimerkein ja isoilla kirjaimilla.",
    "no": "Diktering med tegnsetting og store bokstaver.",
    "el": "Υπαγόρευση με στίξη και κεφαλαία γράμματα.",
    "tr": "Noktalama ve büyük harflerle dikte.",
    "he": "הכתבה עם פיסוק ואותיות גדולות.",
    "ar": "الإملاء مع علامات الترقيم والحروف الكبيرة.",
    "fa": "دیکته با نشانه‌گذاری و حروف بزرگ.",
    "hi": "विराम चिह्न और बड़े अक्षरों के साथ श्रुतलेख।",
    "th": "การถอดเสียงพร้อมเครื่องหมายวรรคตอนและตัวพิมพ์ใหญ่",
    "vi": "Đọc chính tả với dấu câu và chữ viết hoa.",
    "zh": "听写时使用标点符号和大小写。",
    "ja": "句読点と大文字を使った音声入力。",
    "ko": "구두점과 대문자를 사용한 받아쓰기.",
}


def _repo_id(name):
    """Hugging Face repo id for a short model name."""
    if "/" in name:
        return name
    return REPO_OVERRIDES.get(name, f"Systran/faster-whisper-{name}")


# A model folder is a CTranslate2 conversion: ``model.bin`` starts with a
# uint32 binary version, a length-prefixed model name and a uint32 revision,
# and the tokenizer, vocabulary and config sit beside it. Without the
# tokenizer faster-whisper would fetch one from the Hub, so the folder is
# refused instead of silently going online.
CT2_BINARY_HEADER_S = 10       # version (4) + name length (2) + revision (4)
CT2_BINARY_VERSION_MAX = 100
CT2_NAME_MAX = 128


def _model_path(value: str) -> str | None:
    """The folder a ``model`` value names, or None for a catalog name/repo id.

    A value is a folder when it is absolute, drive-relative, starts with
    ``~`` or a dot segment, contains a backslash, or already exists as a
    directory. A one-slash repo id such as ``Systran/faster-whisper-large-v3``
    is not: it only counts as a folder when that folder exists on disk.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    expanded = os.path.expandvars(os.path.expanduser(raw))
    drive, _tail = os.path.splitdrive(expanded)
    looks_local = (os.path.isabs(expanded) or bool(drive) or "\\" in expanded
                   or expanded.startswith(".") or os.path.isdir(expanded))
    if not looks_local:
        return None
    return os.path.abspath(expanded)


def _model_bin_problem(path: str) -> str | None:
    """Why ``model.bin`` is not a CTranslate2 weight file, or None.

    An interrupted download leaves a zero-byte file, and a saved error page
    leaves HTML; both are named here instead of reaching CTranslate2 as a
    traceback. The header is read structurally, never pinned to one binary
    version, so an older or newer conversion still passes.
    """
    try:
        size = os.path.getsize(path)
    except OSError as error:
        return f"model.bin is not readable: {error}"
    if size == 0:
        return "model.bin is empty"
    if size < CT2_BINARY_HEADER_S:
        return f"model.bin is only {size} bytes, shorter than a CTranslate2 header"
    with open(path, "rb") as handle:
        head = handle.read(6)
        version = int.from_bytes(head[:4], "little")
        name_len = int.from_bytes(head[4:6], "little")
        raw_name = handle.read(name_len)
        revision = handle.read(4)
    if not 1 <= version <= CT2_BINARY_VERSION_MAX:
        return "model.bin does not start like a CTranslate2 model"
    if not 2 <= name_len <= CT2_NAME_MAX:
        return "model.bin does not start like a CTranslate2 model"
    if len(raw_name) != name_len or len(revision) != 4:
        return "model.bin header is truncated"
    if raw_name[-1] != 0 or not all(32 <= byte < 127 for byte in raw_name[:-1]):
        return "model.bin does not start like a CTranslate2 model"
    return None


def _model_folder_problem(folder: str) -> str | None:
    """Why the folder cannot serve as a CTranslate2 Whisper model, or None."""
    if not os.path.isdir(folder):
        if os.path.exists(folder):
            return "the path is not a folder"
        return "the folder does not exist"
    model_bin = os.path.join(folder, "model.bin")
    if not os.path.isfile(model_bin):
        return "model.bin is missing"
    problem = _model_bin_problem(model_bin)
    if problem:
        return problem
    config_path = os.path.join(folder, "config.json")
    if not os.path.isfile(config_path):
        return "config.json is missing"
    try:
        with open(config_path, encoding="utf-8") as handle:
            config_data = json.load(handle)
    except (OSError, ValueError) as error:
        return f"config.json is not valid JSON: {error}"
    if not isinstance(config_data, dict):
        return "config.json is not a JSON object"
    if not os.path.isfile(os.path.join(folder, "tokenizer.json")):
        return ("tokenizer.json is missing; without it faster-whisper would "
                "download a tokenizer")
    if not (os.path.isfile(os.path.join(folder, "vocabulary.json"))
            or os.path.isfile(os.path.join(folder, "vocabulary.txt"))):
        return "vocabulary.json or vocabulary.txt is missing"
    return None


def _resolve_model(name, log):
    """Path or repo id to load: a configured folder first, then models/<name>
    inside the service folder, the shared HF cache second, a fresh download
    into models/<name> last — the installation stays self-contained.

    A folder is never turned into a repo id and never downloaded: the folder
    is validated here, and a missing or broken ``model.bin`` raises
    ``ModelPathInvalid`` with the file named.
    """
    folder = _model_path(name)
    if folder is not None:
        problem = _model_folder_problem(folder)
        if problem:
            raise ModelPathInvalid(
                f"model folder {folder} is not a CTranslate2 model: {problem}")
        log.info("Using model folder %s", folder)
        return folder, True

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
    except Exception:  # an offline load can fail in third-party code
        if _model_path(name) is not None:
            # A folder has no online fallback: loading it must never touch
            # the network, so the engine's own error reaches the caller.
            raise
        return WhisperModel(source, device=device, compute_type=compute_type,
                            local_files_only=False)


def _prompt(cfg):
    """Initial prompt: the pinned language's punctuation preset, nothing else.

    User terms never go here: ``initial_prompt`` seeds the first 30 s window
    only, because faster-whisper resets the prompt after every window. The
    dictionary and hotwords travel in ``_hotwords`` instead and reach each
    window through the library's ``hotwords`` parameter.
    """
    lang = cfg.get("language")
    if lang and lang != "auto":
        return PROMPT_PRESETS.get(lang)
    return None


def _hotwords(cfg, token_counter=None):
    """The per-window prompt terms: user hotwords plus dictionary targets.

    Ranked by how often each correction was made (corrections.json) and
    trimmed once to the prompt budget, so every call, and every window of
    it, sees the same string.
    """
    import dictionary
    import dictlearn
    merged = dictionary.merge_hotwords(cfg.get("hotwords"),
                                       cfg.get("dictionary"),
                                       dictlearn.frequency(),
                                       token_counter)
    return merged or None


def _token_counter(model):
    """Exact Whisper token count for one term, when the model is loaded.

    The library's own tokenizer is a BPE with no cheap public estimator, so a
    loaded model counts exactly; before that, ``dictionary.estimate_tokens``
    keeps selection conservative.
    """
    tokenizer = getattr(model, "hf_tokenizer", None)
    if tokenizer is None or not hasattr(tokenizer, "encode"):
        return None

    def count(term):
        return len(tokenizer.encode(" " + term.strip()).ids)

    return count


def _log_windows(segments, log, hotwords):
    """Yield segments unchanged, logging the hotword line per 30 s window.

    The line is the visible proof that the terms reached each window: a bare
    ``initial_prompt`` would cover the first one only. A segment's own
    ``seek`` (its frame offset inside the call) names the window, so no
    boundary is guessed from timestamps.
    """
    terms = len([t for t in str(hotwords or "").split(",") if t.strip()])
    seen = set()
    for segment in segments:
        seek = getattr(segment, "seek", None)
        if seek not in seen:
            seen.add(seek)
            log.info("ASR window %d: hotwords %d terms", len(seen), terms)
        yield segment


def _segment(seg: Any, offset: float = 0.0) -> Segment:
    """One engine segment as our shape, its confidence reading kept.

    ``offset`` shifts the segment and its words by a block start; the
    metadata is copied, never derived from the shifted times.
    """
    words = None
    if getattr(seg, "words", None):
        words = [Word(start=offset + float(w.start),
                      end=offset + float(w.end),
                      text=str(w.word),
                      probability=getattr(w, "probability", None))
                 for w in seg.words]
    return Segment(start=offset + float(seg.start),
                   end=offset + float(seg.end),
                   text=str(seg.text or ""),
                   words=words,
                   avg_logprob=getattr(seg, "avg_logprob", None),
                   compression_ratio=getattr(seg, "compression_ratio", None),
                   no_speech_prob=getattr(seg, "no_speech_prob", None),
                   temperature=getattr(seg, "temperature", None))


def _words_from(words: list[Word] | None, start: float) -> list[Word] | None:
    """The words still ahead of ``start``; None when nothing is left.

    A deduped segment is clipped so the previous pass's text is not repeated.
    Its words follow the clip: the subtitle cue is cut from the words, and a
    word the earlier pass already handed out would repeat its text in the
    cue. A word straddling the clip stays whole - its opening was never
    delivered on its own.
    """
    if not words:
        return None
    kept = [word for word in words if float(word.end) > start]
    return kept or None


def _threshold_failure(segment: Segment, language: str) -> str | None:
    """The reading that sends one batched segment to the sequential path.

    The thresholds are the sequential call's own: ``log_prob`` -1.0 and
    ``no_speech`` 0.6 for every language, the ratio at the calibrated 1.35
    only in the languages the sequential call passes it to
    (``hallucination.ENGINE_RATIO_LANGUAGES``) and at the library default
    elsewhere. A quiet window whose logprob is low too is what the sequential
    path skips as silence; the retry lets its own verdict stand instead of
    trusting the batched one. ``None`` means the segment passes.
    """
    no_speech = segment.no_speech_prob
    logprob = segment.avg_logprob
    quiet = no_speech is not None and float(no_speech) > NO_SPEECH_THRESHOLD
    low = logprob is not None and float(logprob) < LOG_PROB_THRESHOLD
    if quiet and low:
        return (f"no_speech_prob {float(no_speech):.2f} > "
                f"{NO_SPEECH_THRESHOLD:g} with avg_logprob "
                f"{float(logprob):.2f} < {LOG_PROB_THRESHOLD:g}")
    limit = (hallucination.COMPRESSION_RATIO_THRESHOLD
             if language in hallucination.ENGINE_RATIO_LANGUAGES
             else COMPRESSION_RATIO_DEFAULT)
    ratio = segment.compression_ratio
    if ratio is not None and float(ratio) > limit:
        return f"compression_ratio {float(ratio):.2f} > {limit:g}"
    if low:
        return f"avg_logprob {float(logprob):.2f} < {LOG_PROB_THRESHOLD:g}"
    return None


def quality_preset(cfg) -> tuple[str, int, tuple[float, ...] | float]:
    """The final dictation pass settings read from ``quality_preset``.

    Returns the effective preset name, beam size and temperature. Normal keeps
    beam 5 and the 0.0 -> 0.2 fallback ladder, accurate uses beam 10 at one
    temperature, fast decodes greedily at one temperature. A missing, empty
    or unknown value reads as normal, so an old config without the key and a
    hand-edited typo keep working instead of failing the pass.

    This is the only place the final dictation pass takes its beam and
    temperature from: the settings window exposes no raw temperature that
    could override the preset.
    """
    name = str((cfg or {}).get("quality_preset") or "").strip().casefold()
    if name not in QUALITY_PRESETS:
        name = QUALITY_PRESET_DEFAULT
    beam, temperature = QUALITY_PRESET_PARAMS[name]
    return name, beam, temperature


class ASR:
    def __init__(self, cfg, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.model = None
        self._lock = threading.Lock()
        self._batched = None        # one BatchedInferencePipeline per model
        self._batch_error = None    # why the batched path is unavailable
        # The last final dictation pass, confidence readings included; a live
        # partial never overwrites it. The service reads it to decide between
        # a paste and the panel draft.
        self.last_segments: list[Segment] = []
        # The last final take's language reading: what the audio sounded
        # like, never what recognition used. A partial never touches it.
        self.last_language_hint: dict[str, Any] | None = None

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

    def _without_invented(self, segments: list[Segment]) -> list[Segment]:
        """The take without invented text; a suspect loop stays, named.

        The guard drops what the engine itself reads as non-speech and the
        stop-list or the ratio confirms; a loop the engine still reads as
        speech is kept and logged, because cutting a real repeated phrase on
        the ratio alone would lose the F0 reference.
        """
        kept: list[Segment] = []
        for segment in segments:
            verdict = hallucination.judge(segment)
            if verdict is None:
                kept.append(segment)
                continue
            action, reason = verdict
            if action == hallucination.DROP:
                self.log.warning("Dropped invented text at %.1f-%.1f s: %s",
                                 segment.start, segment.end, reason)
                continue
            self.log.warning("Suspect text at %.1f-%.1f s: %s",
                             segment.start, segment.end, reason)
            kept.append(segment)
        return kept

    def _transcribe_impl(self, audio: np.ndarray, language: str, *,
                         word_timestamps: bool = False,
                         initial_prompt: str | None = None,
                         temperature: float | list[float] | tuple[float, ...] | None = None,
                         beam_size: int = 5,
                         log_windows: bool = False):
        """One call to the model with the shared VAD, prompt and guards.

        Dictation and the file mode both enter here, so the anti-hallucination
        settings and the prompt can never drift apart. An explicit prompt or
        temperature from a file job overrides the configured default; omitted
        values keep today's behavior. A temperature sequence reaches
        faster-whisper as a sequence: a scalar means one decode, no fallback.

        The silence guard (``hallucination_silence_threshold``) needs word
        timestamps; the library ignores it otherwise, and the per-block retry
        in ``transcribe_array`` already covers the alignment crash it can
        trip (faster-whisper 1.2.1, issue #1342). The ratio threshold goes
        only to the language it is calibrated on: its metric is UTF-8 bytes,
        and 1.35 fails clean Cyrillic speech at every temperature.

        The term list travels as ``hotwords``, which the library re-applies to
        every window; ``initial_prompt`` stays the language preset or the
        caller's own hint. ``log_windows`` adds one line per window for the
        dictation path, where a live check reads the service log.
        """
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if language in hallucination.ENGINE_RATIO_LANGUAGES:
            options["compression_ratio_threshold"] = (
                hallucination.COMPRESSION_RATIO_THRESHOLD)
        hotwords = _hotwords(self.cfg, _token_counter(self.model))
        if hotwords:
            self.log.debug("ASR hotwords: %s", hotwords)
        segments, info = self.model.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            initial_prompt=(initial_prompt if initial_prompt is not None
                            else _prompt(self.cfg)),
            hotwords=hotwords,
            condition_on_previous_text=False,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            hallucination_silence_threshold=hallucination.HALLUCINATION_SILENCE_S,
            vad_filter=True,
            vad_parameters=dict(VAD_PARAMETERS),
            word_timestamps=word_timestamps,
            **options,
        )
        if log_windows:
            segments = _log_windows(segments, self.log, hotwords)
        return segments, info

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
        the sequential path; only the window scheduling differs. The wrapper
        accepts the three quality thresholds and applies none of them
        (faster-whisper 1.2.1), so ``transcribe_array`` checks every returned
        segment against the same thresholds and re-decodes a failing one on
        the sequential path (``_threshold_failure``); the post-filter in
        ``emit`` remains the guard for invented text.
        """
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = float(temperature)
        if language in hallucination.ENGINE_RATIO_LANGUAGES:
            options["compression_ratio_threshold"] = (
                hallucination.COMPRESSION_RATIO_THRESHOLD)
        hotwords = _hotwords(self.cfg, _token_counter(self.model))
        if hotwords:
            self.log.debug("ASR hotwords: %s", hotwords)
        return self._batched.transcribe(
            audio,
            language=language,
            beam_size=5,
            initial_prompt=(initial_prompt if initial_prompt is not None
                            else _prompt(self.cfg)),
            hotwords=hotwords,
            condition_on_previous_text=False,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            log_prob_threshold=LOG_PROB_THRESHOLD,
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

    def _retry_batched_segment(self, block: np.ndarray, segment: Segment,
                               block_start: float, language: str, *,
                               word_timestamps: bool,
                               initial_prompt: str | None,
                               temperature: float | None) -> list[Segment]:
        """One batched segment decoded again by the sequential path.

        The slice is the segment's own span inside the block; the sequential
        call runs the block's prompt, word-timestamp setting and temperature,
        so its own thresholds, fallback ladder and silence skip decide. Times
        come back absolute in the file, ready for the caller's dedupe. An
        empty list means the sequential path read the span as silence.
        """
        rate = float(TARGET_RATE)
        first = max(0, int(round((segment.start - block_start) * rate)))
        last = min(block.shape[0],
                   int(round((segment.end - block_start) * rate)))
        if last <= first:
            return [segment]
        with self._lock:
            rows, _info = self._transcribe_impl(
                block[first:last], language,
                word_timestamps=word_timestamps,
                initial_prompt=initial_prompt, temperature=temperature)
            return [_segment(row, block_start + first / rate) for row in rows]

    def transcribe(self, audio: np.ndarray, verbose: bool = True, *,
                   partial: bool = False) -> str:
        """Transcribe 16 kHz float32 mono audio; returns stripped text.

        A live partial reads a short window (see audio.partial_snapshot) and
        decodes greedily at one temperature. The final dictation pass takes
        its beam and temperature from the ``quality_preset`` config value
        (see ``quality_preset``): normal keeps beam 5 and the two-step
        fallback ladder, accurate reads wider at one temperature, fast
        decodes greedily at one temperature. Word timestamps stay on, and the
        panel draft marks the words the engine is unsure about.

        The final pass keeps the engine's own readings on ``last_segments``
        (see ``transcript.Segment``); the service decides from them whether
        the phrase may be pasted or must wait in the panel draft for review.
        A partial never touches them: it is a preview, not a take.

        The final pass is also where system audio lands, and a film's music
        is a known source of invented text: a segment the engine reads as
        non-speech whose text is a stop phrase or a loop is dropped before
        the service sees it. A partial keeps the raw preview.
        """
        with self._lock:
            t0 = time.time()
            lang = self._language()
            if partial:
                segments, info = self._transcribe_impl(
                    audio, lang, beam_size=PARTIAL_BEAM_SIZE,
                    temperature=PARTIAL_TEMPERATURE)
            else:
                preset, beam, temperature = quality_preset(self.cfg)
                segments, info = self._transcribe_impl(
                    audio, lang, beam_size=beam, word_timestamps=True,
                    temperature=temperature, log_windows=True)
                self.log.info("Final pass preset: %s (beam %d)",
                              preset, beam)
            collected = [_segment(seg) for seg in segments]
            if not partial:
                collected = self._without_invented(collected)
            text = "".join(seg.text for seg in collected).strip()
            if not partial:
                self.last_segments = collected
                self.last_language_hint = (
                    self._detect_language_hint(audio, lang) if text else None)
            if verbose:
                self.log.info(
                    "Transcription: audio %.1f s, recognition %.2f s, lang=%s",
                    audio.shape[0] / 16000.0, time.time() - t0, info.language,
                )
            return text

    def retranscribe(self, audio: np.ndarray) -> str:
        """Read one take again with a wider search: beam 10, temperature 0.

        The draft's retry pass (card-247): the model is already loaded, so
        the take is one locked call on the live engine — no second model, no
        download, no model switch. A single temperature means no fallback
        ladder: the reading is the best beam search at the confident setting.
        The same hallucination post-filter as the dictation pass runs, and
        ``last_segments`` becomes this pass, so the caller marks doubt on the
        text this call returned rather than on the first take.
        """
        with self._lock:
            t0 = time.time()
            lang = self._language()
            segments, info = self._transcribe_impl(
                audio, lang, beam_size=RETRANSCRIBE_BEAM_SIZE,
                temperature=RETRANSCRIBE_TEMPERATURE, word_timestamps=True)
            collected = self._without_invented(
                [_segment(seg) for seg in segments])
            text = "".join(seg.text for seg in collected).strip()
            self.last_segments = collected
            self.log.info(
                "Retranscription: audio %.1f s, recognition %.2f s, lang=%s, "
                "beam=%d", audio.shape[0] / float(TARGET_RATE),
                time.time() - t0, info.language, RETRANSCRIBE_BEAM_SIZE)
            return text

    def _detect_language_hint(self, audio: np.ndarray,
                              pinned: str) -> dict[str, Any] | None:
        """The language hint for one final take; a failure never costs it.

        The pinned language is handed to the engine as the recognition
        language, so the call's own ``language_probability`` is 1 by
        construction and says nothing about the take. The honest reading is a
        detection pass over the same audio: it never changes recognition, it
        only names what the take sounds like. The caller already holds the
        model lock.
        """
        detect = getattr(self.model, "detect_language", None)
        engine = getattr(self.model, "model", self.model)
        if detect is None or getattr(engine, "is_multilingual", True) is False:
            return None
        try:
            _detected, _probability, probs = detect(audio)
        except Exception as exc:  # noqa: BLE001 - a hint never costs a take
            self.log.debug("Language detection: %s", exc)
            return None
        hint = language_hint(probs, pinned)
        if hint is None:
            return None
        hint["ts"] = time.time()
        self.log.info("Language hint: pinned %s %.0f%%, sounds like %s %.0f%%",
                      pinned, float(hint["pinned_prob"]) * 100.0,
                      hint["other"], float(hint["other_prob"]) * 100.0)
        return hint

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
                         clip_timestamps: str | list[float] | None = None,
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

        ``clip_timestamps`` limits recognition to the named ranges: only the
        samples inside them reach the engine, and segment times stay absolute
        in the file. The ranges are clamped to ``duration``, so a range past
        the end never produces a timestamp past it.

        ``batched=True`` runs each block through ``BatchedInferencePipeline``;
        a failure falls back to the sequential path for the rest of the job,
        with ``last_end`` keeping the transcript free of duplicates. A block
        whose word alignment trips faster-whisper 1.2.1 (issue #1342) is
        decoded again without word timestamps: the retry is logged, the
        segments it produces carry no words (the ones handed out before the
        crash keep theirs), and the setting stays on for the blocks that
        follow.

        Every segment passes the hallucination guard before it is yielded: a
        window the engine reads as non-speech whose text is a stop phrase or
        a loop is dropped and logged, on both the sequential and the batched
        path. A suspect loop the engine still reads as speech is kept and
        logged; only real speech may survive a repeated phrase.

        The seam between blocks carries context: the last
        ``FILE_CONTEXT_TAIL_CHARS`` characters of the block just decoded go
        into the next block's ``initial_prompt``, on the sequential and the
        batched path alike, so a name or a style decided in one block
        survives into the next. The tail is bounded and replaces the previous
        one instead of accumulating; it is cleared when a clip range jumps,
        so an unrelated piece never inherits another piece's words, and a
        block that produced no speech keeps the last tail it had.
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
        clips = parse_clip_timestamps(clip_timestamps, total)
        pieces: list[tuple[int, int]] = []
        if clips:
            for start, end in clips:
                first = min(samples, int(round(start * TARGET_RATE)))
                last = min(samples, int(round(end * TARGET_RATE)))
                for offset in range(first, last, block_samples):
                    pieces.append((offset, min(offset + block_samples, last)))
        else:
            pieces = [(offset, min(offset + block_samples, samples))
                      for offset in range(0, samples, block_samples)]
        blocks = len(pieces)
        size = self._batch_size(batch_size)
        use_batched = bool(batched)
        if use_batched and self._get_batched_pipeline() is None:
            use_batched = False
        self.log.info("File transcription started: %.1f s, lang=%s, blocks=%d, "
                      "batched=%s", total, lang, blocks, use_batched)

        last_end = 0.0       # end of the last segment handed to the caller
        last_progress = 0.0
        fallback_used = False
        # The hint every block starts from: the job's own prompt, else the
        # configured language preset. The seam tail is joined per block.
        base_prompt = (initial_prompt if initial_prompt is not None
                       else _prompt(self.cfg))
        context_tail = ""    # the previous block's ending, bounded
        previous_end: int | None = None

        def report(value: float) -> None:
            nonlocal last_progress
            if on_progress is None:
                return
            value = min(99.0, max(0.0, value))
            if value < last_progress:
                value = last_progress
            last_progress = value
            on_progress(value)

        def deliver(raw: Segment, dedupe: bool,
                    collected: list[str]) -> Iterator[Segment]:
            """Judge, dedupe and yield one segment as the caller sees it.

            ``collected`` keeps the text that actually reached the caller, so
            a dropped invented line never enters the next block's hint. A
            clipped start takes the segment's words with it: the subtitle cue
            is cut from the words, and text the previous pass already gave
            must not come back inside a cue.
            """
            nonlocal last_end
            text = (raw.text or "").strip()
            verdict = hallucination.judge(raw)
            if verdict is not None:
                action, reason = verdict
                if action == hallucination.DROP:
                    # Invented text on silence or music is cut here: the
                    # engine's own readings say the window is non-speech
                    # and the text is a known false phrase or a loop.
                    self.log.warning(
                        "Dropped invented text at %.1f-%.1f s: %s",
                        raw.start, raw.end, reason)
                    if total > 0:
                        report(raw.end / total * 100.0)
                    return
                self.log.warning("Suspect text at %.1f-%.1f s: %s",
                                 raw.start, raw.end, reason)
            start = raw.start
            end = raw.end
            if dedupe:
                if end <= last_end:
                    return
                start = max(start, last_end)
            if text:
                if end < start:
                    end = start
                last_end = max(last_end, end)
                collected.append(text)
                yield replace(raw, start=start, end=end, text=text,
                              words=_words_from(raw.words, start))
            if total > 0:
                report(end / total * 100.0)

        def emit(segments, block_start: float, dedupe: bool,
                 collected: list[str], block: np.ndarray, batched: bool,
                 prompt: str | None, word_ts: bool) -> Iterator[Segment]:
            """Yield one block's segments, optionally skipping what was seen.

            On the batched path every segment is checked against the same
            thresholds the sequential call applies: one that fails is decoded
            again by the sequential engine over its own span, and the log
            names the reading that sent it. The replacement passes the same
            dedupe and hallucination guard as any other segment; a span the
            sequential pass reads as silence produces nothing.
            """
            while True:
                if should_stop is not None and should_stop():
                    raise AsrCancelled()
                self._pause(should_pause, should_stop)
                with self._lock:
                    try:
                        seg = next(segments)
                    except StopIteration:
                        return
                raw = _segment(seg, block_start)
                failure = _threshold_failure(raw, lang) if batched else None
                if failure is None:
                    yield from deliver(raw, dedupe, collected)
                    continue
                self.log.warning(
                    "Batched segment %.1f-%.1f s failed a threshold (%s); "
                    "re-recognizing it on the sequential path",
                    raw.start, raw.end, failure)
                rows = self._retry_batched_segment(
                    block, raw, block_start, lang,
                    word_timestamps=word_ts, initial_prompt=prompt,
                    temperature=temperature)
                if not rows:
                    self.log.info("Sequential retry at %.1f-%.1f s: no speech",
                                  raw.start, raw.end)
                for row in rows:
                    yield from deliver(row, dedupe, collected)
                if total > 0:
                    report(raw.end / total * 100.0)

        def open_stream(block_audio: np.ndarray, word_ts: bool,
                        language: str, batch_size: int,
                        prompt: str | None):
            """One block's segment iterator: the batched path, else sequential.

            The model lock is taken only for opening; ``emit`` holds it per
            segment, so a dictation never waits for a whole block.
            """
            if use_batched:
                with self._lock:
                    return self._transcribe_batched(
                        block_audio, language, word_timestamps=word_ts,
                        initial_prompt=prompt,
                        temperature=temperature, batch_size=batch_size)
            with self._lock:
                return self._transcribe_impl(
                    block_audio, language, word_timestamps=word_ts,
                    initial_prompt=prompt, temperature=temperature)

        for index, (offset, piece_end) in enumerate(pieces):
            if should_stop is not None and should_stop():
                raise AsrCancelled()
            self._pause(should_pause, should_stop)
            block = audio[offset:piece_end]
            block_start = offset / float(TARGET_RATE)
            # A clip range that jumps starts a new hint chain: the previous
            # piece is not what the caller will hear next.
            if previous_end is None or offset != previous_end:
                context_tail = ""
            prompt = _file_prompt(base_prompt, context_tail)
            if context_tail:
                self.log.debug("File block %d: %d chars of the previous block "
                               "as the prompt", index + 1, len(context_tail))
            # Word timestamps are retried per block, never switched off for
            # the whole job: only the block whose alignment trips loses them.
            block_words = word_timestamps
            block_text: list[str] = []
            dedupe = fallback_used
            language_logged = index != 0
            while True:
                block_text.clear()
                try:
                    segments, info = open_stream(block, block_words,
                                                 lang, size, prompt)
                    if not language_logged:
                        self.log.info("File transcription engine language: %s",
                                      info.language)
                        language_logged = True
                    yield from emit(segments, block_start, dedupe=dedupe,
                                    collected=block_text, block=block,
                                    batched=use_batched, prompt=prompt,
                                    word_ts=block_words)
                    break
                except AsrCancelled:
                    raise
                except Exception as exc:
                    if isinstance(exc, IndexError) and block_words:
                        # faster-whisper 1.2.1 find_alignment trips on a
                        # trailing sliver of speech (issue #1342) while the
                        # iterator is consumed; that iterator is dead, so the
                        # block is decoded again without word timestamps.
                        # What the failed pass already handed out keeps its
                        # words; the retried remainder has none, and this line
                        # is the mark: it names the retry and the crash.
                        self.log.warning(
                            "Word timestamps failed on block %d (%s: %s); "
                            "retrying the block without word timestamps",
                            index + 1, type(exc).__name__, exc)
                        block_words = False
                        dedupe = True
                        continue
                    if not use_batched:
                        raise
                    self.log.warning("Batched pipeline failed: %s; falling back to the sequential path", exc)
                    use_batched = False
                    fallback_used = True
                    dedupe = True
            # A silent block says nothing about the next one: keep the tail.
            if block_text:
                context_tail = file_context_tail(" ".join(block_text))
            previous_end = piece_end
        if on_progress is not None:
            on_progress(100.0)

    def transcribe_file(self, path: str, *, language: str | None = None,
                        word_timestamps: bool = False,
                        initial_prompt: str | None = None,
                        temperature: float | None = None,
                        clip_timestamps: str | list[float] | None = None,
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
            clip_timestamps=clip_timestamps,
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
