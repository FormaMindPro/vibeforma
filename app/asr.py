"""ASR engine: one WhisperModel per service lifetime, multilingual dictation."""
import gc
import logging
import threading
import time

import numpy as np

from config import MODELS_DIR, bootstrap_cuda_dlls

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
    return REPO_OVERRIDES.get(name, "Systran/faster-whisper-%s" % name)


def _resolve_model(name, log):
    """Path or repo id to load: models/<name> inside the service folder
    first, the shared HF cache second, a fresh download into models/<name>
    last — the installation stays self-contained."""
    import os

    local = os.path.join(MODELS_DIR, name)
    if os.path.exists(os.path.join(local, "model.bin")):
        return local, True  # local dir: no hub access at all
    repo = _repo_id(name)
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo, local_files_only=True)
        return name, True   # already in the shared HF cache
    except Exception:
        pass
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
    except Exception:
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
    hotwords = (cfg.get("hotwords") or "").strip()
    if hotwords:
        parts.append(hotwords)
    return " ".join(parts) or None


class ASR:
    def __init__(self, cfg, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.model = None
        self._lock = threading.Lock()

    def load(self):
        """Load once; keep in memory for the whole service lifetime."""
        bootstrap_cuda_dlls()

        with self._lock:
            t0 = time.time()
            self.log.info(
                "Loading model %s: device=%s compute_type=%s",
                self.cfg["model"], self.cfg["device"], self.cfg["compute_type"],
            )
            self.model = _make_model(self.cfg["model"], self.cfg["device"],
                                     self.cfg["compute_type"], self.log)
            self.log.info("Model loaded in %.1f s", time.time() - t0)

    def transcribe(self, audio: np.ndarray, verbose: bool = True) -> str:
        """Transcribe 16 kHz float32 mono audio; returns stripped text.

        language "auto" -> None: faster-whisper detects it per phrase.
        """
        with self._lock:
            t0 = time.time()
            lang = self.cfg.get("language")
            segments, info = self.model.transcribe(
                audio,
                language=None if not lang or lang == "auto" else lang,
                beam_size=5,
                initial_prompt=_prompt(self.cfg),
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 250, "speech_pad_ms": 30},
            )
            text = "".join(s.text for s in segments).strip()
            if verbose:
                self.log.info(
                    "Transcription: audio %.1f s, recognition %.2f s, lang=%s",
                    audio.shape[0] / 16000.0, time.time() - t0, info.language,
                )
            return text

    def reload_model(self, name) -> bool:
        """Swap the model in place (tray menu); returns success."""
        with self._lock:
            try:
                old = self.model
                self.model = None
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
