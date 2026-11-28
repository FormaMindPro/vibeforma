"""Configuration: config.json load/save, defaults, device lookup, DLL bootstrap."""
import contextlib
import json
import os
from typing import Any

import dictionary
import snippets

VERSION = "0.5.9"

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


def _test_wav_path() -> str:
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

# Final dictation pass presets (card-265): the default and the accepted
# codes. The engine maps each code to its beam and temperature in
# ``asr.quality_preset``; a missing or unknown value reads as the default, so
# an old config and a hand-edited typo keep the shipped behavior.
QUALITY_PRESET_DEFAULT = "normal"
QUALITY_PRESETS = ("normal", "accurate", "fast")

# Whisper models offered in the UI (menu order) come from the catalog.
# The redundant alias marks the re-export explicit: strict mypy
# (no_implicit_reexport) hides imported names from other modules unless the
# module re-exports them on purpose; at runtime the alias binds the same name.
from model_catalog import DEFAULT_MODEL as DEFAULT_MODEL  # noqa: E402
from model_catalog import MODEL_GB as MODEL_GB  # noqa: E402
from model_catalog import MODELS as MODELS  # noqa: E402

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
    "audio_source": "microphone",       # microphone | system
    # System audio takes are written as WAV with TXT and SRT beside them
    # (card-262); empty means Documents\VibeForma\Recordings.
    "system_recordings_dir": "",
    "model": "large-v3-turbo",          # catalog name, HF repo id, or a CT2 model folder
    # Saved local CT2 folders (card-264): [{name, path}] with the name and an
    # absolute path only. Validity is computed when the list is drawn; an old
    # config without this key reads as an empty list.
    "model_folders": [],
    "device": "cuda",
    "compute_type": "float16",
    "language": "en",                   # pinned exactly: a LANGS code, never guessed
    "locale": "auto",                   # UI language: auto = follow Windows
    "quality_preset": QUALITY_PRESET_DEFAULT,  # final dictation: normal | accurate | fast
    "hotwords": "",                     # terms appended to the prompt (user-local)
    "dictation_context": False,         # previous phrase + selected text as hotwords (opt-in)
    "dictionary": [],                   # {"from", "to"} pairs, post-ASR replacements
    "snippets": [],                     # {"trigger", "text"} pairs, post-ASR expansion
    "app_profiles": [],                 # per-app dictionary and snippets (card-251)
    "text_rules": False,                # backtrack and spoken lists (opt-in)
    "text_cleanup": False,              # numbers, fillers, capitals (opt-in)
    "history": False,                   # keep a local dictation history (opt-in)
    "history_app": False,               # remember the target app in the history
    "history_audio": False,             # keep the WAV of each dictation (opt-in)
    "auto_stop": True,
    "silence_ms": 2000,                 # phrase-closing pause
    "continuous": True,                 # long dictation: no max_record_s cut; chunks at pauses
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
    "actions": [],                      # voice actions: whole phrase -> command/model/agent

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
    # Whisper models from Hugging Face (card-273): a background pass compares
    # the repo's commit sha with the sidecar marker of each catalog folder
    # under MODELS_DIR; the update itself is always by consent.
    "model_update_check": True,         # background model revision check
    "model_update_interval_h": 24,      # hours between model revision checks
    "model_keep_old": True,             # keep the previous model on disk (fast switching)
    "onboarding_done": False,           # the first-run wizard has run
}


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # A hand-edited or half-written file can hold a non-object (null, a
        # number, a list, a string). json.load accepts those, but update()
        # would raise TypeError, so a non-dict reads as a missing file.
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# -- by-app profiles (card-251) ---------------------------------------------
# One profile binds a window to its own dictionary and snippets: the
# process name ("Code.exe" or "code"), a window-title fragment, or both. A
# take is matched by the address captured at recording start, so the
# profile follows the window the person spoke into, not the one active later.
MAX_APP_PROFILES = 50
MAX_PROFILE_NAME = 80
MAX_PROFILE_PROCESS = 120
MAX_PROFILE_WINDOW = 200


def _profile_text(value: object) -> str:
    """A profile string field, stripped; anything else is empty."""
    return value.strip() if isinstance(value, str) else ""


def _clean_app_profile(entry: object) -> dict[str, Any] | None:
    """One profile as a dict, or None when it names no target.

    The dictionary and the snippets use the same normalization as the global
    lists, so a profile can never carry a malformed pair. A missing name
    falls back to the target; the settings tab shows that name.
    """
    if not isinstance(entry, dict):
        return None
    process = _profile_text(entry.get("process"))
    window = _profile_text(entry.get("window"))
    if not process and not window:
        return None
    name = _profile_text(entry.get("name")) or process or window
    return {
        "name": name[:MAX_PROFILE_NAME],
        "process": process[:MAX_PROFILE_PROCESS],
        "window": window[:MAX_PROFILE_WINDOW],
        "dictionary": dictionary.normalize(entry.get("dictionary")),
        "snippets": snippets.normalize(entry.get("snippets")),
    }


def normalize_app_profiles(
        profiles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Clean the by-app profile list: targets kept, names deduped, capped."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in profiles or []:
        profile = _clean_app_profile(item)
        if profile is None:
            continue
        key = profile["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(profile)
        if len(out) >= MAX_APP_PROFILES:
            break
    return out


def validate_app_profiles(
        profiles: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Format problems in the raw profile list: [{level, code, profile}].

    Codes: "format" (not an object, or neither a process nor a window
    target) and "duplicate" (the name repeats an earlier profile). The
    broken entries are dropped by normalization; the report exists for the
    settings tab, so a typo never disappears silently.
    """
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in profiles or []:
        profile = _clean_app_profile(item)
        if profile is None:
            name = _profile_text(item.get("name")) if isinstance(item, dict) else ""
            issues.append({"level": "error", "code": "format", "profile": name})
            continue
        key = profile["name"].casefold()
        duplicate = key in seen
        seen.add(key)
        if duplicate:
            issues.append({"level": "error", "code": "duplicate",
                           "profile": profile["name"]})
    return issues


def _exe_stem(value: object) -> str:
    """A process name without a folder or the ".exe" suffix, casefolded."""
    base = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    base = base.casefold()
    return base[:-4] if base.endswith(".exe") else base


def app_profile_matches(profile: dict[str, Any], process: str = "",
                        title: str = "") -> bool:
    """True when every target the profile names matches this window.

    A profile with only a process ignores the title; one with only a window
    fragment ignores the process; both set must both match. An empty window
    or process never satisfies a rule that needs it.
    """
    wanted_process = _exe_stem(profile.get("process"))
    wanted_window = str(profile.get("window") or "").casefold()
    if not wanted_process and not wanted_window:
        return False
    if wanted_process and _exe_stem(process) != wanted_process:
        return False
    return not (wanted_window and wanted_window not in str(title or "").casefold())


def match_app_profile(profiles: list[dict[str, Any]] | None,
                      process: str = "", title: str = "") -> dict[str, Any] | None:
    """The first profile this window matches, or None (global rules apply)."""
    for profile in normalize_app_profiles(profiles):
        if app_profile_matches(profile, process, title):
            return profile
    return None


def bootstrap_cuda_dlls() -> None:
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


def find_input_device(substring: str | None) -> int | None:
    """Device index by name substring, else the system default input."""
    import sounddevice as sd

    default_in: int | None = None
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


def input_device_names() -> list[str]:
    """All capture-capable device names, in sounddevice order."""
    import sounddevice as sd

    out: list[str] = []
    for d in sd.query_devices():
        if d.get("max_input_channels", 0) > 0:
            out.append(str(d["name"]))
    return out
