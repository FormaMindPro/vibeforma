"""HUD panel geometry: the height must follow the text, not the previous height."""
import hud


def test_panel_height_is_base_plus_growth():
    assert hud.panel_height(hud.BASE_H, hud.BASE_TEXT_LINES) == hud.BASE_H
    assert (hud.panel_height(hud.BASE_H, hud.BASE_TEXT_LINES + 3)
            == hud.BASE_H + 3 * hud.LINE_H)


def test_panel_height_does_not_accumulate_over_calls():
    for want in (5, 8, 4, 12, 4, 7, 12, 4):
        got = hud.panel_height(hud.BASE_H, want)
    assert got == hud.BASE_H + (4 - hud.BASE_TEXT_LINES) * hud.LINE_H


def test_panel_height_respects_dpi_scaled_values():
    got = hud.panel_height(285, 7, base=4, line_h=30)
    assert got == 285 + 3 * 30


def test_panel_height_includes_the_action_row():
    got = hud.panel_height(hud.BASE_H, hud.BASE_TEXT_LINES,
                           extra_h=hud.ACTION_ROW_H)
    assert got == hud.BASE_H + hud.ACTION_ROW_H
    got = hud.panel_height(hud.BASE_H, hud.BASE_TEXT_LINES + 2,
                           extra_h=hud.ACTION_ROW_H)
    assert got == hud.BASE_H + hud.ACTION_ROW_H + 2 * hud.LINE_H


def test_placeholder_keeps_the_base_height():
    assert hud.panel_lines("\u2026") == hud.BASE_TEXT_LINES
    assert hud.panel_lines("") == hud.BASE_TEXT_LINES


def test_file_panel_adds_its_height_to_the_root():
    assert hud.FILE_PANEL_H > 0
    got = hud.panel_height(hud.BASE_H, hud.BASE_TEXT_LINES,
                           extra_h=hud.FILE_PANEL_H)
    assert got == hud.BASE_H + hud.FILE_PANEL_H


def test_long_text_is_capped():
    assert hud.panel_lines("word " * 500) == hud.MAX_TEXT_LINES
    assert hud.panel_lines("\n" * 40) == hud.MAX_TEXT_LINES


def test_wrapped_lines_counts_real_wrapping():
    # cpl=46: a 47-char line wraps to two, a 46-char line stays one
    assert hud.wrapped_lines("a" * 46) == 1
    assert hud.wrapped_lines("a" * 47) == 2
    assert hud.wrapped_lines("a\nb") == 2
    assert hud.wrapped_lines("") == 1


def test_short_phrase_stays_at_base():
    assert hud.panel_lines("\u041f\u0440\u0438\u0432\u0435\u0442, \u043c\u0438\u0440") \
        == hud.BASE_TEXT_LINES


def test_draft_minimum_sits_between_base_and_cap():
    assert hud.DRAFT_MIN_LINES == 6
    assert hud.BASE_TEXT_LINES < hud.DRAFT_MIN_LINES < hud.MAX_TEXT_LINES


def test_draft_short_text_reserves_the_minimum():
    assert hud.panel_lines("hi", minimum=hud.DRAFT_MIN_LINES) \
        == hud.DRAFT_MIN_LINES
    assert hud.panel_lines("", minimum=hud.DRAFT_MIN_LINES) \
        == hud.DRAFT_MIN_LINES


def test_draft_medium_text_grows_line_by_line():
    assert hud.panel_lines("\n" * 7, minimum=hud.DRAFT_MIN_LINES) == 8


def test_draft_long_text_still_stops_at_the_cap():
    assert hud.panel_lines("word " * 500, minimum=hud.DRAFT_MIN_LINES) \
        == hud.MAX_TEXT_LINES


def test_panel_height_honours_the_draft_minimum():
    got = hud.panel_height(hud.BASE_H, hud.BASE_TEXT_LINES,
                           minimum=hud.DRAFT_MIN_LINES)
    assert got == hud.BASE_H \
        + (hud.DRAFT_MIN_LINES - hud.BASE_TEXT_LINES) * hud.LINE_H


def test_progress_starts_at_zero_and_runs_fast():
    assert hud.progress_percent(0) == 0
    assert hud.progress_percent(-1) == 0
    assert hud.progress_percent(0.2) >= 60
    assert hud.progress_percent(0.5) >= 85


def test_progress_never_reaches_hundred_on_its_own():
    values = [hud.progress_percent(t) for t in (0.1, 0.3, 0.6, 1, 2, 10, 1e6)]
    assert values == sorted(values)
    assert values[-1] <= hud.PROG_CAP
    assert values[-1] < 100


def test_progress_cap_is_respected():
    assert hud.progress_percent(1e6, cap=80) == 80
    assert hud.progress_percent(0.5, cap=50) == 50


def test_progress_ramp_controls_the_start():
    assert hud.progress_percent(0.2, ramp=0.5) < hud.progress_percent(0.2)
    assert hud.progress_percent(0.2, ramp=0.01) > 85
    assert hud.progress_percent(0.2, ramp=hud.PROG_RAMP_S) \
        == hud.progress_percent(0.2)


