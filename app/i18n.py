"""UI strings loader: locales/*.json, en fallback, OS auto-detection.

Adding a language = dropping app/locales/<code>.json with the same keys.
No code edits needed; the settings window lists files it finds.
"""
import ctypes
import json
import os

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
_CACHE = {}

# Windows LCID -> locale code (extend as locales land)
_LCID = {0x0419: "ru", 0x0409: "en"}


def available_locales():
    try:
        return sorted(f[:-5] for f in os.listdir(LOCALES_DIR)
                      if f.endswith(".json"))
    except OSError:
        return ["en"]


def system_locale():
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFFFF
        return _LCID.get(lcid, "en")
    except Exception:  # noqa: BLE001 - ctypes locale lookup has no stable base exception
        return "en"


def _load_file(locale):
    path = os.path.join(LOCALES_DIR, f"{locale}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load(locale):
    if locale not in _CACHE:
        strings = dict(_load_file("en"))       # en fallback under everything
        strings.update(_load_file(locale))     # chosen locale on top
        _CACHE[locale] = strings
    return _CACHE[locale]


def resolve(cfg):
    """cfg locale 'auto' -> OS language; unknown codes fall back to en."""
    loc = (cfg or {}).get("locale", "auto")
    if loc in ("auto", "", None):
        loc = system_locale()
    return loc if loc in available_locales() else "en"


def t(cfg, key, **kw):
    s = _load(resolve(cfg)).get(key, key)
    if kw:
        s = s.format(**kw)
    return s
