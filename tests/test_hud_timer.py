"""HUD take timer: formatting, limit/elapsed, colour limits, idle.

The timer is a pure view over the live state: nothing here opens a window
or an audio device, so the file runs on Linux too. The effective limit is
the service's own rule: continuous dictation is never cut by
``max_record_s`` and therefore counts up like a system take.
"""
import json
from pathlib import Path

import hud
from vibeforma import service as service_mod

LOCALES = Path(__file__).resolve().parents[1] / "app" / "locales"
MINUS = "\u2212"
START = 1000.0


def _state(**over):
    st = {"state": "recording", "record_started": START,
          "record_source": "microphone", "recording_path": "",
          "record_limit_s": 0.0}
    st.update(over)
    return st


def _limited(**over):
    return _state(record_limit_s=120.0, **over)


def _load(lang):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        return json.load(handle)


# -- formatting ----------------------------------------------------------
def test_minutes_and_seconds():
    assert hud.format_clock(0) == "00:00"
    assert hud.format_clock(59) == "00:59"
    assert hud.format_clock(60) == "01:00"


def test_hours_switch_after_one_hour():
    assert hud.format_clock(3599) == "59:59"
    assert hud.format_clock(3600) == "1:00:00"
    assert hud.format_clock(3661) == "1:01:01"


def test_a_negative_duration_reads_as_zero():
    assert hud.format_clock(-5) == "00:00"
    assert hud.format_clock(-0.5) == "00:00"


def test_the_column_fits_every_form():
    longest = MINUS + hud.format_clock(3661)
    assert len(longest) == hud.TIMER_WIDTH
    assert len(MINUS + hud.format_clock(59)) <= hud.TIMER_WIDTH


# -- microphone with a real limit: time left -----------------------------
def test_limited_microphone_counts_down_from_the_limit():
    view = hud.timer_view(_limited(), now=START)
    assert view["visible"] is True
    assert view["system"] is False
    assert view["limited"] is True
    assert view["text"] == MINUS + "02:00"
    assert view["remaining"] == 120.0


def test_limited_microphone_rounds_the_second_up():
    view = hud.timer_view(_limited(), now=START + 2.4)
    # 117.6 s left reads 01:58, never 01:57: the whole second is shown.
    assert view["text"] == MINUS + "01:58"
    assert round(view["remaining"], 1) == 117.6


def test_limited_microphone_never_goes_below_zero():
    view = hud.timer_view(_limited(), now=START + 500)
    assert view["text"] == MINUS + "00:00"
    assert view["remaining"] == 0.0


def test_a_sub_second_limit_still_reads_one():
    view = hud.timer_view(_state(record_limit_s=0.5), now=START)
    assert view["text"] == MINUS + "00:01"


# -- microphone without a limit (continuous): elapsed --------------------
def test_continuous_microphone_counts_up():
    view = hud.timer_view(_state(), now=START + 65)
    assert view["visible"] is True
    assert view["system"] is False
    assert view["limited"] is False
    assert view["text"] == "01:05"
    assert view["remaining"] is None


def test_continuous_microphone_has_no_minus_and_no_warning_colour():
    view = hud.timer_view(_state(), now=START + 115)
    assert view["text"] == "01:55"
    assert not view["text"].startswith(MINUS)
    assert view["color"] == hud.TIMER_FG


def test_continuous_microphone_never_shows_a_negative_elapsed():
    assert hud.timer_view(_state(), now=START - 5)["text"] == "00:00"


# -- system audio: elapsed -----------------------------------------------
def test_system_counts_up():
    st = _state(record_source="system", recording_path="C:/rec/a.wav")
    view = hud.timer_view(st, now=START + 3)
    assert view["visible"] is True
    assert view["system"] is True
    assert view["limited"] is False
    assert view["text"] == "00:03"
    assert view["remaining"] is None


def test_system_keeps_counting_past_an_hour():
    st = _state(record_source="system", recording_path="C:/rec/a.wav")
    assert hud.timer_view(st, now=START + 3661)["text"] == "1:01:01"


def test_system_ignores_a_published_limit():
    st = _state(record_source="system", recording_path="C:/rec/a.wav",
                record_limit_s=120.0)
    view = hud.timer_view(st, now=START + 115)
    assert view["text"] == "01:55"
    assert view["color"] == hud.TIMER_FG


def test_a_written_file_names_the_system_source():
    # The source key is the direct fact; a take recorded before it existed
    # still reads as system because only a system take writes a file.
    st = _state(recording_path="C:/rec/a.wav")
    del st["record_source"]
    assert hud.timer_view(st, now=START + 61)["text"] == "01:01"


# -- colour limits -------------------------------------------------------
def test_colour_turns_at_thirty_and_ten():
    assert hud.timer_color(120) == hud.TIMER_FG
    assert hud.timer_color(30) == hud.TIMER_FG
    assert hud.timer_color(29.9) == hud.TIMER_WARN
    assert hud.timer_color(10) == hud.TIMER_WARN
    assert hud.timer_color(9.9) == hud.TIMER_ALERT
    assert hud.timer_color(0) == hud.TIMER_ALERT


def test_view_colour_follows_the_remaining_time():
    assert hud.timer_view(_limited(), now=START + 90)["color"] == hud.TIMER_FG
    assert hud.timer_view(_limited(), now=START + 90.1)["color"] \
        == hud.TIMER_WARN
    assert hud.timer_view(_limited(), now=START + 110)["color"] \
        == hud.TIMER_WARN
    assert hud.timer_view(_limited(), now=START + 110.1)["color"] \
        == hud.TIMER_ALERT
    assert hud.timer_view(_limited(), now=START + 500)["color"] \
        == hud.TIMER_ALERT


def test_elapsed_clocks_never_take_the_warning_colour():
    # 115 s into a 120 s (unlimited) take would be 5 s left if a limit were
    # claimed: the continuous clock must stay neutral and count up.
    assert hud.timer_view(_state(), now=START + 115)["color"] == hud.TIMER_FG
    st = _state(record_source="system", recording_path="C:/rec/a.wav")
    assert hud.timer_view(st, now=START + 115)["color"] == hud.TIMER_FG


# -- idle: hidden --------------------------------------------------------
def test_idle_states_hide_the_timer():
    for state in ("idle", "loading", "processing", "paused", "error",
                  "updating"):
        view = hud.timer_view({"state": state}, now=START)
        assert view["visible"] is False, state
        assert view["text"] == ""
        assert view["color"] == hud.TIMER_FG


def test_recording_without_a_start_time_hides_the_timer():
    assert hud.timer_view(_state(record_started=0.0))["visible"] is False
    assert hud.timer_view(_state(record_started=None))["visible"] is False
    assert hud.timer_view(_state(record_started="soon"))["visible"] is False


# -- the service's effective limit ---------------------------------------
def test_continuous_dictation_has_no_limit():
    assert service_mod.take_limit_s(
        {"continuous": True, "max_record_s": 120}) == 0.0
    assert service_mod.take_limit_s({"max_record_s": 120}) == 0.0


def test_a_cut_take_keeps_its_limit():
    assert service_mod.take_limit_s(
        {"continuous": False, "max_record_s": 120}) == 120.0


def test_missing_or_unusable_settings_give_no_limit():
    assert service_mod.take_limit_s({}) == 0.0
    assert service_mod.take_limit_s({"continuous": False}) == 0.0
    assert service_mod.take_limit_s(
        {"continuous": False, "max_record_s": "x"}) == 0.0
    assert service_mod.take_limit_s(
        {"continuous": False, "max_record_s": -5}) == 0.0


# -- texts ---------------------------------------------------------------
def test_timer_texts_exist_in_both_locales():
    en, ru = _load("en"), _load("ru")
    for key in ("hud.timer_left", "hud.timer_take_time"):
        assert str(en.get(key, "")).strip(), key
        assert str(ru.get(key, "")).strip(), key
    assert "{max}" in en["hud.timer_left"]
    assert "{max}" in ru["hud.timer_left"]
    # the renamed key leaves no stale entry behind
    assert "hud.timer_elapsed" not in en
    assert "hud.timer_elapsed" not in ru
