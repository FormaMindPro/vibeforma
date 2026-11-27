"""One reference for the shipped models: names, sizes and wording.

Both the service (config, settings) and the installer read this file, so a
model is added or renamed in exactly one place. The wording lives in the
locale JSON files (``models.desc.<name>``).
"""
from __future__ import annotations

import json
import os

MODELS = ("large-v3", "large-v3-turbo", "medium", "small")
MODEL_GB = {"large-v3": 3.1, "large-v3-turbo": 1.6, "medium": 1.5, "small": 0.5}
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_MODEL_CPU = "small"

# Canonical CT2 conversions that live outside Systran (Systran has no turbo
# repo; mobiuslabsgmbh/faster-whisper-large-v3-turbo redirects to dropbox-dash).
REPO_OVERRIDES = {
    "large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
}


def repo_id(name: str) -> str:
    """Hugging Face repo id for a short model name."""
    if "/" in name:
        return name
    return REPO_OVERRIDES.get(name, f"Systran/faster-whisper-{name}")

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


def description(name: str, lang: str = "en") -> str:
    """Model blurb from the shipped locale; English is the fallback."""

    def blurb(code: str) -> str:
        path = os.path.join(LOCALES_DIR, code + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                return str(json.load(f).get(f"models.desc.{name}") or "")
        except (OSError, ValueError):
            return ""

    return blurb(lang) or blurb("en") or ""
