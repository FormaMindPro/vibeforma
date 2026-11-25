"""Dependency smoke: wetext is installed and turns spoken numbers into digits.

The opt-in text cleanup converts dictated numbers with wetext's inverse text
normalization. This test proves only the dependency itself: the locked wheel is
importable, its bundled models load, and ru/en ITN read "twenty three" as 23.
The cleanup pipeline that calls it is a separate card.
"""

from __future__ import annotations

import importlib.metadata

import pytest
import wetext
from wetext import Normalizer

LOCKED_VERSION = "0.2.1"
# "twenty three" in Cyrillic letters; kept as escapes: the source stays ASCII.
TWENTY_THREE_RU = "\u0434\u0432\u0430\u0434\u0446\u0430\u0442\u044c \u0442\u0440\u0438"
SPOKEN_TWENTY_THREE = {"ru": TWENTY_THREE_RU, "en": "twenty three"}


def test_wetext_package_imports():
    assert callable(wetext.Normalizer)


def test_wetext_version_matches_the_lock():
    assert importlib.metadata.version("wetext") == LOCKED_VERSION


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_spoken_twenty_three_becomes_digits(lang):
    normalized = Normalizer(lang=lang, operator="itn").normalize(SPOKEN_TWENTY_THREE[lang])
    assert normalized == "23"
