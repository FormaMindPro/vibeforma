"""Window geometry for the settings dialog: fit the content, stay on screen.

Pure helpers (no Tk): the dialog is sized to the tallest tab once, clamped
to the screen so nothing is cut, and centred on the monitor.
"""
MIN_W = 620
MIN_H = 480
PREFERRED_H = 680
SCREEN_MARGIN_X = 40
SCREEN_MARGIN_Y = 80


def fit_size(req_w: int, req_h: int, screen_w: int, screen_h: int,
             scale: float = 1.0) -> tuple[int, int]:
    """The window size: at least the minimum, at most the screen minus margin.

    The height also stops at the preferred size so a tall tab scrolls
    instead of turning the dialog into a full-screen column; the margin
    leaves room for the taskbar and keeps the window draggable. The minimum
    wins over both caps so a tiny screen still shows the dialog.
    """
    min_w = round(MIN_W * scale)
    min_h = round(MIN_H * scale)
    room_w = max(min_w, screen_w - round(SCREEN_MARGIN_X * scale))
    room_h = max(min_h, screen_h - round(SCREEN_MARGIN_Y * scale))
    cap_h = max(min_h, round(PREFERRED_H * scale))
    return (min(max(min_w, int(req_w)), room_w),
            min(max(min_h, int(req_h)), room_h, cap_h))


def place_xy(width: int, height: int, screen_w: int,
             screen_h: int) -> tuple[int, int]:
    """Centred on the screen; never negative on a screen smaller than the window."""
    return max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2)
