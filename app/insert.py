"""Insert text into the focused field: clipboard + paste, optional restore."""
import logging
import threading
import time

import pyperclip
from pynput.keyboard import Controller, Key

PASTE_DELAY_S = 0.03   # before the paste, let the clipboard settle
AFTER_DELAY_S = 0.06   # after the paste so slow apps register it
RESTORE_DELAY_S = 1.0  # give the target app time to read the clipboard


def foreground_window() -> str:
    """Title of the window that will receive the paste (for the log)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.strip()
        return title[:80] if title else "(untitled)"
    except Exception:
        return "(unknown)"


def get_foreground_hwnd():
    """HWND of the current foreground window (captured at recording start)."""
    try:
        import ctypes
        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return None


def restore_foreground(hwnd):
    """Best-effort: bring the recorded window back before pasting."""
    if not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() == hwnd:
            return
        # ALT keystroke unlocks SetForegroundWindow from a background process
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.15)
    except Exception:
        pass


def insert_text(text: str, restore_clipboard: bool = True,
                log: logging.Logger | None = None, target_hwnd=None,
                paste_method: str = "shift_insert"):
    prev = None
    try:
        prev = pyperclip.paste()
    except Exception as e:
        if log:
            log.warning("Failed to read the clipboard: %s", e)
    pyperclip.copy(text)

    restore_foreground(target_hwnd)
    target = foreground_window()
    kb = Controller()
    time.sleep(PASTE_DELAY_S)
    if paste_method == "shift_insert":
        # Layout-independent: Ctrl+V under a Russian layout resolves to
        # Ctrl+"м" and never pastes (measured 2026-11-19); Shift+Insert works
        # on every layout.
        with kb.pressed(Key.shift):
            kb.press(Key.insert)
            kb.release(Key.insert)
    else:  # "ctrl_v"
        with kb.pressed(Key.ctrl):
            kb.press("v")
            kb.release("v")
    time.sleep(AFTER_DELAY_S)
    if log:
        log.info("Pasted %d chars — window: %s", len(text), target)

    if restore_clipboard and prev:
        threading.Thread(target=_restore, args=(prev,), daemon=True).start()


def _restore(prev: str):
    time.sleep(RESTORE_DELAY_S)
    try:
        pyperclip.copy(prev)
    except Exception:
        pass
