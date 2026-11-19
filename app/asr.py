"""ASR engine: one WhisperModel per service lifetime, Russian transcription."""
import gc
import logging
import threading
import time

import numpy as np

from config import MODELS_DIR, bootstrap_cuda_dlls

# Рантайм без сети: кэшированная модель грузится offline-first; сеть — только
# для первичной закачки модели, которой ещё нет в кэше.

# Канонические CT2-конверсии вне Systran (у Systran turbo-репозитория нет;
# mobiuslabsgmbh/faster-whisper-large-v3-turbo редиректит в dropbox-dash).
REPO_OVERRIDES = {
    "large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
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
    log.info("Модель %s скачивается в %s (первичная установка)", name, local)
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
                "Загрузка модели %s: device=%s compute_type=%s",
                self.cfg["model"], self.cfg["device"], self.cfg["compute_type"],
            )
            self.model = _make_model(self.cfg["model"], self.cfg["device"],
                                     self.cfg["compute_type"], self.log)
            self.log.info("Модель загружена за %.1f с", time.time() - t0)

    def transcribe(self, audio: np.ndarray, verbose: bool = True) -> str:
        """Transcribe 16 kHz float32 mono audio; returns stripped text."""
        with self._lock:
            t0 = time.time()
            segments, info = self.model.transcribe(
                audio,
                language=self.cfg["language"],
                beam_size=5,
                initial_prompt=(self.cfg.get("initial_prompt") or None),
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 250, "speech_pad_ms": 30},
            )
            text = "".join(s.text for s in segments).strip()
            if verbose:
                self.log.info(
                    "Транскрипция: аудио %.1f с, распознавание %.2f с, lang=%s",
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
                self.log.info("Переключение модели на %s", name)

                t0 = time.time()
                self.model = _make_model(name, self.cfg["device"],
                                         self.cfg["compute_type"], self.log)
                self.log.info("Модель %s загружена за %.1f с", name, time.time() - t0)
                return True
            except Exception as e:
                self.log.exception("Переключение модели не удалось: %s", e)
                return False
