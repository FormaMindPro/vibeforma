"""Prompt presets: every menu language has punctuation and capitalization.

Whisper's ``initial_prompt`` seeds the first window with the language preset;
a language missing from ``PROMPT_PRESETS`` silently dictates without
punctuation. The key set is compared by name against the UI language menu
(``config.LANG_NAMES``), so dropping any one language fails the test.
"""
import re

import asr
import config
import pytest

MENU_LANGUAGES = set(config.LANG_NAMES) - {"auto"}

# Languages whose writing system is distinctive enough to check mechanically;
# Latin-script presets are left to the human verdict.
NATIVE_SCRIPT = {
    "el": re.compile("[\u0370-\u03ff\u1f00-\u1fff]"),
    "he": re.compile("[\u0590-\u05ff]"),
    "ar": re.compile("[\u0600-\u06ff\u0750-\u077f]"),
    "fa": re.compile("[\u0600-\u06ff\u0750-\u077f]"),
    "hi": re.compile("[\u0900-\u097f]"),
    "th": re.compile("[\u0e00-\u0e7f]"),
    "zh": re.compile("[\u3400-\u4dbf\u4e00-\u9fff]"),
    "ja": re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]"),
    "ko": re.compile("[\uac00-\ud7af\u1100-\u11ff]"),
}


def test_every_menu_language_has_its_own_preset():
    missing = sorted(MENU_LANGUAGES - set(asr.PROMPT_PRESETS))
    extra = sorted(set(asr.PROMPT_PRESETS) - MENU_LANGUAGES)
    assert not missing, f"languages without a prompt preset: {missing}"
    assert not extra, f"presets outside the language menu: {extra}"


def test_every_preset_is_a_non_empty_phrase():
    empty = sorted(lang for lang, text in asr.PROMPT_PRESETS.items()
                   if not str(text).strip())
    assert not empty, f"empty prompt presets: {empty}"


def test_the_prompt_of_each_language_is_reachable():
    for lang in sorted(MENU_LANGUAGES):
        assert asr._prompt({"language": lang}) == asr.PROMPT_PRESETS[lang]


def test_auto_and_missing_languages_keep_no_preset():
    assert asr._prompt({"language": "auto"}) is None
    assert asr._prompt({}) is None


@pytest.mark.parametrize(("lang", "pattern"), sorted(NATIVE_SCRIPT.items()))
def test_a_non_latin_preset_is_written_in_its_own_script(lang, pattern):
    text = asr.PROMPT_PRESETS[lang]
    assert pattern.search(text), f"{lang} preset is not in its own script: {text!r}"
