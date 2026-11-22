"""Settings dialog geometry: the window fits the content and stays on screen."""
import window_layout as wl


def test_minimum_size_wins_over_small_content():
    assert wl.fit_size(300, 200, 1920, 1080) == (wl.MIN_W, wl.MIN_H)


def test_content_larger_than_minimum_is_kept():
    width = wl.MIN_W + 100
    assert wl.fit_size(width, 600, 1920, 1080) == (width, 600)


def test_tall_content_stops_at_the_preferred_height():
    width = wl.MIN_W + 100
    assert wl.fit_size(width, 3000, 1920, 1080) == (width, wl.PREFERRED_H)


def test_oversized_content_is_clamped_to_the_screen():
    w, h = wl.fit_size(4000, 3000, 1920, 1080)
    assert w == 1920 - wl.SCREEN_MARGIN_X
    assert h <= 1080 - wl.SCREEN_MARGIN_Y


def test_scaled_minimum_and_margins():
    w, h = wl.fit_size(100, 100, 1707, 960, scale=1.5)
    assert (w, h) == (round(wl.MIN_W * 1.5), round(wl.MIN_H * 1.5))
    w, h = wl.fit_size(4000, 4000, 1707, 960, scale=1.5)
    assert w <= 1707 - round(wl.SCREEN_MARGIN_X * 1.5)
    assert h <= 960 - round(wl.SCREEN_MARGIN_Y * 1.5)


def test_centered_on_screen():
    assert wl.place_xy(800, 600, 1920, 1080) == (560, 240)


def test_never_negative_on_a_small_screen():
    assert wl.place_xy(1200, 900, 600, 500) == (0, 0)
