"""Configuration: config.json load/save, defaults, device lookup, DLL bootstrap."""
import json
import os

VERSION = "0.1.2"

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SERVICE_DIR, "config.json")
LOG_DIR = os.path.join(SERVICE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "service.log")
TEST_WAV = os.path.join(SERVICE_DIR, "test_ru.wav")
LOCK_PATH = os.path.join(SERVICE_DIR, "service.lock")
MODELS_DIR = os.path.join(SERVICE_DIR, "models")   # models live inside the service folder

# Whisper understands ~99 languages; these are offered in the UI.
# The chosen code is used exactly — no per-phrase guessing.
LANGS = (
    "en", "ru", "uk", "de", "fr", "es", "it", "pt", "nl", "pl", "cs",
    "sv", "da", "fi", "no", "el", "tr", "he", "ar", "fa", "hi", "th", "vi",
    "zh", "ja", "ko",
)

# Native names (endonyms) shown in the UI: every language is written in
# that language itself.
LANG_NAMES = {
    "en": "English", "ru": "Русский", "uk": "Українська", "de": "Deutsch",
    "fr": "Français", "es": "Español", "it": "Italiano", "pt": "Português",
    "nl": "Nederlands", "pl": "Polski", "cs": "Čeština", "sv": "Svenska",
    "da": "Dansk", "fi": "Suomi", "no": "Norsk", "el": "Ελληνικά",
    "tr": "Türkçe", "he": "עברית", "ar": "العربية", "fa": "فارسی",
    "hi": "हिन्दी", "th": "ไทย", "vi": "Tiếng Việt", "zh": "中文",
    "ja": "日本語", "ko": "한국어",
}

DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "input_device": "",                 # substring of the device name
    "model": "large-v3-turbo",
    "device": "cuda",
    "compute_type": "float16",
    "language": "en",                   # pinned exactly: a LANGS code, never guessed
    "locale": "auto",                   # UI language: auto = follow Windows
    "hotwords": "",                     # terms appended to the prompt (user-local)
    "auto_stop": True,
    "silence_ms": 2000,                 # phrase-closing pause
    "max_record_s": 120,
    "partial_every_s": 1.5,             # live partial transcribe period (HUD)
    "hud": True,                        # live monitoring panel
    "hud_alpha": 0.94,                  # panel transparency
    "hud_autogrow": True,               # panel grows with the partial text
    "beep_on": True,
    "beep_off": True,
    "restore_clipboard": True,
    "paste_method": "shift_insert",     # shift_insert | ctrl_v
    "autostart": True,
    "http_api": False,                  # optional: POST 127.0.0.1:8787/transcribe
    "http_port": 8787,
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def bootstrap_cuda_dlls():
    """Make pip cuDNN/cuBLAS wheels visible to ctranslate2 on Windows."""
    import glob
    import sys

    if sys.platform != "win32":
        return
    site = os.path.join(sys.prefix, "Lib", "site-packages")
    dirs = glob.glob(os.path.join(site, "nvidia", "*", "bin"))
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


def find_input_device(substring):
    """Device index by name substring, else the system default input."""
    import sounddevice as sd

    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    needle = (substring or "").strip().lower()
    if not needle:
        return default_in
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0 and needle in str(d["name"]).lower():
            return i
    return default_in


def input_device_names():
    """All capture-capable device names, in sounddevice order."""
    import sounddevice as sd

    out = []
    for d in sd.query_devices():
        if d.get("max_input_channels", 0) > 0:
            out.append(str(d["name"]))
    return out
