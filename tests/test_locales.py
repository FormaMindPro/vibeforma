"""Locale parity: en/ru keys and {placeholders} must match."""
import json
from pathlib import Path
from string import Formatter

import pytest

LOCALES = Path(__file__).resolve().parents[1] / "app" / "locales"
LANGS = ("en", "ru")


def _load(lang):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def catalogs():
    return {lang: _load(lang) for lang in LANGS}


def test_en_and_ru_have_the_same_keys(catalogs):
    assert set(catalogs["en"]) == set(catalogs["ru"])


def test_no_empty_values(catalogs):
    for lang, strings in catalogs.items():
        empty = [key for key, value in strings.items() if not str(value).strip()]
        assert not empty, f"{lang} has empty values: {empty}"


def _placeholders(value):
    return {name for _, name, _, _ in Formatter().parse(str(value)) if name}


def test_placeholders_match_between_languages(catalogs):
    en, ru = catalogs["en"], catalogs["ru"]
    mismatched = []
    for key in sorted(set(en) & set(ru)):
        if _placeholders(en[key]) != _placeholders(ru[key]):
            mismatched.append((key, sorted(_placeholders(en[key])),
                               sorted(_placeholders(ru[key]))))
    assert not mismatched, f"placeholder drift: {mismatched}"


def test_t_survives_foreign_placeholder_sharp_edge(monkeypatch):
    """A typo'd placeholder in a third-party locale must not raise in the UI."""
    import i18n

    broken = "Press {hotkeyy} to start"
    monkeypatch.setitem(i18n._CACHE, "en", {"status.line": broken})
    out = i18n.t({"locale": "en"}, "status.line", hotkey="Ctrl+Alt+Space")
    assert out == broken

    # a valid placeholder still formats after the guard landed
    monkeypatch.setitem(i18n._CACHE, "en",
                        {"status.line": "Press {hotkey} to start"})
    assert i18n.t({"locale": "en"}, "status.line",
                  hotkey="Ctrl+Alt+Space") == "Press Ctrl+Alt+Space to start"
