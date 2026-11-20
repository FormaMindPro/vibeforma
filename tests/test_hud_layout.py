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


def test_placeholder_keeps_the_base_height():
    assert hud.panel_lines("\u2026") == hud.BASE_TEXT_LINES
    assert hud.panel_lines("") == hud.BASE_TEXT_LINES


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
