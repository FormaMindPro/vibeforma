"""Configuration: config.json load/save, defaults, device lookup, DLL bootstrap."""
import contextlib
import json
import os

VERSION = "0.4.0"

SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
# The service folder holds run.cmd and main.py both in a source checkout
# (app/) and in an installed copy (app/ copied to the target root). The venv
# sits beside it in a checkout and inside it after an install. A harness
# reads these paths from location.json and never guesses the install folder.
INSTALL_DIR = SERVICE_DIR
LAUNCHER_PATH = os.path.join(INSTALL_DIR, "run.cmd")
ENTRY_PATH = os.path.join(INSTALL_DIR, "main.py")


def _venv_python() -> str:
    for base in (INSTALL_DIR, os.path.dirname(INSTALL_DIR)):
        candidate = os.path.join(base, ".venv", "Scripts", "python.exe")
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(INSTALL_DIR, ".venv", "Scripts", "python.exe")


PYTHON_PATH = _venv_python()
# Where the user's data lives. A source install keeps it beside the code; the
# packed build sets VIBEFORMA_DATA_DIR (LOCALAPPDATA\VibeForma) so config,
# models, logs and history survive an in-place upgrade and a reinstall.
DATA_DIR = os.environ.get("VIBEFORMA_DATA_DIR") or SERVICE_DIR
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "service.log")


def _test_wav_path():
    """The self-test WAV beside the service, or at the repository root.

    An installed copy keeps app/test_ru.wav; a fresh git checkout keeps the
    file at the root, so --selftest works there too.
    """
    beside = os.path.join(SERVICE_DIR, "test_ru.wav")
    if os.path.isfile(beside):
        return beside
    parent = os.path.join(os.path.dirname(SERVICE_DIR), "test_ru.wav")
    return parent if os.path.isfile(parent) else beside


TEST_WAV = _test_wav_path()
LOCK_PATH = os.path.join(DATA_DIR, "service.lock")
MODELS_DIR = os.path.join(DATA_DIR, "models")   # models live in the data folder

# Whisper understands ~99 languages; these are offered in the UI.
# The chosen code is used exactly — no per-phrase guessing.
LANGS = (
    "en", "ru", "uk", "de", "fr", "es", "it", "pt", "nl", "pl", "cs",
    "sv", "da", "fi", "no", "el", "tr", "he", "ar", "fa", "hi", "th", "vi",
    "zh", "ja", "ko",
)

# Whisper models offered in the UI (menu order) come from the catalog.
from model_catalog import MODEL_GB, MODELS  # noqa: E402,F401 - re-exported

# Native names (endonyms) shown in the UI: every language is written in
# that language itself.
LANG_NAMES = {
    "en": "English", "ru": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
    "uk": "\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430", "de": "Deutsch",
    "fr": "Français", "es": "Español", "it": "Italiano", "pt": "Português",
    "nl": "Nederlands", "pl": "Polski", "cs": "Čeština", "sv": "Svenska",
    "da": "Dansk", "fi": "Suomi", "no": "Norsk", "el": "Ελληνικά",
    "tr": "Türkçe", "he": "עברית", "ar": "العربية", "fa": "فارسی",
    "hi": "हिन्दी", "th": "ไทย", "vi": "Tiếng Việt", "zh": "中文",
    "ja": "日本語", "ko": "한국어",
}

DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "hold_to_talk": False,              # hotkey held while talking, released to stop
    "input_device": "",                 # substring of the device name
    "model": "large-v3-turbo",
    "device": "cuda",
    "compute_type": "float16",
    "language": "en",                   # pinned exactly: a LANGS code, never guessed
    "locale": "auto",                   # UI language: auto = follow Windows
    "hotwords": "",                     # terms appended to the prompt (user-local)
    "dictionary": [],                   # {"from", "to"} pairs, post-ASR replacements
    "snippets": [],                     # {"trigger", "text"} pairs, post-ASR expansion
    "text_rules": False,                # backtrack and spoken lists (opt-in)
    "history": False,                   # keep a local dictation history (opt-in)
    "history_app": False,               # remember the target app in the history
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
    "confirm_focus_change": True,       # ask where to paste if the window changed
    "clipboard_exclude_history": True,  # keep the text out of history and cloud
    "notify_text": False,               # show the phrase in notifications (opt-in)
    "log_window_titles": False,         # write the target window title to the log
    "hud_privacy": False,               # the panel hides the text from the start
    "autostart": True,
    "http_api": False,                  # optional: POST 127.0.0.1:8787/transcribe
    "http_port": 8787,
    "http_token": "",                   # required when http_api is on
    "hook_command": "",                 # external command: text on stdin (opt-in)
    "hook_timeout_s": 5,                # hook run limit, kept inside 1-30 s

    # File mode: the same engine takes a path, not a microphone.
    "file_roots": [],                   # empty: every local fixed/removable disk
    "file_max_mb": 4096,                # input file size limit
    "file_max_s": 6 * 3600,             # input duration limit
    "file_max_sync_s": 900,             # synchronous /v1/audio/transcriptions cap
    "job_keep_s": 24 * 3600,            # how long finished jobs stay in memory
    "file_word_timestamps": False,      # word-level timings (off: segments suffice)
    "file_folder_recursive": False,     # tray folder dialog: include subfolders
    "file_batched": True,               # long file jobs use BatchedInferencePipeline
    "file_batch_size": 8,               # windows per batched pass, 1..16

    "update_check": True,               # background check for newer releases
    "update_interval_h": 2,             # hours between checks
    "auto_update": "notify",            # notify (ask) | auto (explicit opt-in)
    "model_keep_old": True,             # keep the previous model on disk (fast switching)
    "onboarding_done": False,           # the first-run wizard has run
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
    os.makedirs(DATA_DIR, exist_ok=True)
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
        with contextlib.suppress(OSError):
            os.add_dll_directory(d)
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


def find_input_device(substring):
    """Device index by name substring, else the system default input."""
    import sounddevice as sd

    try:
        default_in = sd.default.device[0]
    except Exception:  # noqa: BLE001 - sounddevice has no stable base exception for the default device
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
