"""History table: row cells and phrase filtering."""
import time

import settings_window


def test_row_formats_timestamp_chars_app_and_one_line_text():
    record = {"ts": 1795186994, "text": "first\nsecond", "chars": 12,
              "app": "Editor"}

    stamp, chars, app, text = settings_window.history_row(record)

    assert stamp == time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(1795186994))
    assert chars == 12
    assert app == "Editor"
    assert text == "first second"


def test_row_falls_back_to_text_length():
    _stamp, chars, app, text = settings_window.history_row({"text": "abc"})

    assert chars == 3
    assert app == ""
    assert text == "abc"


def test_search_matches_text_case_insensitively():
    record = {"text": "Нужно сделать таблицу", "app": "ZCode"}

    assert settings_window.history_matches(record, "таблиц")
    assert settings_window.history_matches(record, "zcode")
    assert not settings_window.history_matches(record, "блокнот")


def test_empty_query_matches_everything():
    assert settings_window.history_matches({"text": "x"}, "  ")
