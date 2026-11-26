"""Window chrome that Tk does not expose: rounded corners, dark title bar.

The settings dialog keeps its native frame and asks DWM for rounded
corners and a dark caption (Windows 10 2004+ / Windows 11). The monitoring
panel is borderless, so it gets a rounded window region instead; the region
is rebuilt after every resize. Every call is a no-op outside Windows and
never breaks the window.

Tk hands out the HWND of an inner window; DWM wants the root of that
hierarchy, so every call first walks up with GetAncestor.
"""
from __future__ import annotations

import contextlib
import ctypes
import sys

_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWCP_ROUND = 2
_GA_ROOT = 2


# ponytail: Tk's own DPI reading, one factor per widget; ceiling: base monitor
# scale only; upgrade: GetDpiForWindow per window for mixed-DPI desktops.
def dpi_factor(win) -> float:
    """Windows scaling factor (1.0 at 100%, 1.5 at 150%) for fixed-pixel sizes."""
    try:
        return max(1.0, win.winfo_fpixels("1i") / 96.0)
    except Exception:  # noqa: BLE001 - a closing window must not break a resize
        return 1.0


if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _dwmapi = ctypes.windll.dwmapi
    _gdi32 = ctypes.windll.gdi32
    _user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetAncestor.restype = ctypes.c_void_p
    _user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_bool]
    _dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
    _gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]


def _hwnd(win) -> int:
    win.update_idletasks()
    hwnd = int(win.winfo_id())
    root = _user32.GetAncestor(ctypes.c_void_p(hwnd), _GA_ROOT)
    return int(root or hwnd)


def _set_dwm(win, attribute: int, value: int) -> bool:
    data = ctypes.c_int(value)
    result = _dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(_hwnd(win)), attribute, ctypes.byref(data),
        ctypes.sizeof(data))
    return result == 0


def round_native(win) -> bool:
    """Ask DWM to round a normally framed window (Windows 11)."""
    if sys.platform != "win32":
        return False
    try:
        return _set_dwm(win, _DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def dark_title(win) -> bool:
    """Paint the native title bar dark (Windows 10 2004+ and Windows 11)."""
    if sys.platform != "win32":
        return False
    try:
        for attribute in (_DWMWA_USE_IMMERSIVE_DARK_MODE, 19):
            if _set_dwm(win, attribute, 1):
                return True
        return False
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def _apply_region(win, radius_px: int) -> bool:
    try:
        width = win.winfo_width()
        height = win.winfo_height()
        if width <= 1 or height <= 1:
            return False
        region = _gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius_px * 2, radius_px * 2)
        if not region:
            return False
        if _user32.SetWindowRgn(ctypes.c_void_p(_hwnd(win)), region, True):
            return True
        _gdi32.DeleteObject(ctypes.c_void_p(region))
        return False
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def round_region(win, radius_px: int = 14) -> bool:
    """Round a borderless window by clipping its region (no shadow)."""
    if sys.platform != "win32":
        return False
    return _apply_region(win, radius_px)


def keep_round_region(win, radius_px: int = 14) -> None:
    """Rebuild the clipped region after resizes, debounced and idempotent."""
    if sys.platform != "win32":
        return
    state = {"job": None, "size": (0, 0)}

    def apply(_event=None) -> None:
        if state["job"] is not None:
            with contextlib.suppress(Exception):
                win.after_cancel(state["job"])
        state["job"] = win.after(40, rebuild)

    def rebuild() -> None:
        state["job"] = None
        try:
            size = (win.winfo_width(), win.winfo_height())
        except Exception:  # noqa: BLE001 - the window may be closing
            return
        if size == state["size"]:
            return
        state["size"] = size
        _apply_region(win, radius_px)

    win.bind("<Configure>", apply, add="+")
