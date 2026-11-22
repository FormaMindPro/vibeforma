"""Insert text into the focused field: clipboard + paste, optional restore.

The dictated text is pasted only into the window that was active when the
recording started. If the focus moved, the user is asked where the text
should go (current window / clipboard / cancel) — never a silent paste into
a foreign window. The previous clipboard is restored only when the user has
not changed it in the meantime (GetClipboardSequenceNumber).
"""
import contextlib
import logging
import os
import sys
import threading
import time

import pyperclip
from pynput.keyboard import Controller, Key, KeyCode

PASTE_DELAY_S = 0.03   # before the paste, let the clipboard settle
AFTER_DELAY_S = 0.06   # after the paste so slow apps register it
RESTORE_DELAY_S = 1.0  # give the target app time to read the clipboard
FOCUS_TIMEOUT_S = 30   # the focus dialog closes as "copy" when unanswered
UNDO_VK = 0x5A         # VK_Z: layout-independent Ctrl+Z

RESULT_PASTED = "pasted"
RESULT_COPIED = "copied"
RESULT_CANCELLED = "cancelled"


def copy_text(value: str):
    """Put text into the clipboard (history actions, restore)."""
    pyperclip.copy(value)


def send_undo():
    """Ctrl+Z into the focused field by virtual key, not by a character.

    A character key ("z") resolves through the active layout and may land
    on another letter under a non-Latin layout; VK_Z is the physical key.
    """
    kb = Controller()
    time.sleep(PASTE_DELAY_S)
    with kb.pressed(Key.ctrl):
        kb.press(KeyCode.from_vk(UNDO_VK))
        kb.release(KeyCode.from_vk(UNDO_VK))
    time.sleep(AFTER_DELAY_S)


def foreground_window() -> str:
    """Title of the window that will receive the paste (for the log)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.c_int]
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.strip()
        return title[:80] if title else "(untitled)"
    except Exception:  # noqa: BLE001 - the title lookup fails on unusual windows
        return "(unknown)"


def get_foreground_hwnd():
    """HWND of the current foreground window (captured at recording start)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return user32.GetForegroundWindow()
    except Exception:  # noqa: BLE001 - no ctypes means no foreground window
        return None


def window_process_id(hwnd) -> int:
    """Process id that owns the window; 0 when it cannot be read."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),
                                        ctypes.byref(pid))
        return int(pid.value)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def is_own_window(hwnd) -> bool:
    """True for a window of this process: the panel, settings, dialogs."""
    return bool(hwnd) and window_process_id(hwnd) == os.getpid()


def activate_window(hwnd) -> bool:
    """Bring the picked window forward before a panel-approved paste.

    The monitoring panel is always on top: after a click on one of its
    buttons the panel owns the foreground, while the text has to land in
    the window the user picked. Only a minimized window is restored:
    SW_RESTORE on a maximized window would un-maximize it, which reads as
    the window suddenly shrinking. Returns False when the window refuses
    to come forward (an elevated app, for example).
    """
    if not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_int
        if user32.IsIconic(ctypes.c_void_p(hwnd)):
            user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        time.sleep(0.15)
        return get_foreground_hwnd() == hwnd
    except Exception:  # noqa: BLE001 - activation is best-effort, paste may still work
        return False


def clipboard_sequence() -> int:
    """Windows clipboard change counter; 0 when the platform has none."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        return int(user32.GetClipboardSequenceNumber())
    except (AttributeError, OSError):
        return 0


def exclude_from_clipboard_history() -> bool:
    """Mark the current clipboard content as private for history and cloud.

    Uses the Windows 10+ clipboard formats: presence of
    ExcludeClipboardContentFromMonitorProcessing keeps the item out of the
    clipboard history, CanIncludeInClipboardHistory=0 and
    CanUploadToCloudClipboard=0 keep it out of cloud sync. Returns False
    where the platform or the clipboard does not support it.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return False
        try:
            marked = True
            for name in ("ExcludeClipboardContentFromMonitorProcessing",
                         "CanIncludeInClipboardHistory",
                         "CanUploadToCloudClipboard"):
                marked = _set_clipboard_flag(ctypes, user32, kernel32,
                                             name) and marked
            return marked
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - a clipboard without these formats is unsupported
        return False


def _set_clipboard_flag(ctypes, user32, kernel32, name) -> bool:
    """Set one clipboard format to a DWORD 0; the caller holds the clipboard."""
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.RegisterClipboardFormatW.restype = ctypes.c_uint
    user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.CloseClipboard.argtypes = []

    fmt = user32.RegisterClipboardFormatW(name)
    if not fmt:
        return False
    handle = kernel32.GlobalAlloc(0x0002, 4)  # GMEM_MOVEABLE
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))[0] = 0
    finally:
        kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)
        return False
    return True


def insert_text(text: str, restore_clipboard: bool = True,
                log: logging.Logger | None = None, target_hwnd=None,
                paste_method: str = "shift_insert", confirm_focus=None,
                exclude_history: bool = True, log_title: bool = False,
                undo_state: dict | None = None) -> str:
    """Paste text into the target window; returns one of the RESULT_* values.

    confirm_focus is called only when the foreground window differs from
    target_hwnd; it returns RESULT_PASTED (paste into the current window),
    RESULT_COPIED (leave the text in the clipboard) or RESULT_CANCELLED
    (restore the clipboard). Without a callable the text is copied, never
    pasted into a foreign window. undo_state, when given, receives the
    previous clipboard ("prev") and whether it was readable ("prev_known").
    """
    prev = None
    prev_known = False
    try:
        prev = pyperclip.paste()
        prev_known = True
    except Exception as e:  # noqa: BLE001 - a failed dialog must not abort
        if log:
            log.warning("Failed to read the clipboard: %s", e)
    if undo_state is not None:
        undo_state["prev"] = prev
        undo_state["prev_known"] = prev_known
    pyperclip.copy(text)
    own_seq = clipboard_sequence()
    if exclude_history and not exclude_from_clipboard_history() and log:
        log.info("Clipboard history exclusion is not available here")

    target = foreground_window() if log_title else None
    if target_hwnd and get_foreground_hwnd() != target_hwnd:
        choice = _ask_focus_change(confirm_focus, log)
        if choice == RESULT_COPIED:
            if log:
                log.info("Target window changed — the text is in the clipboard")
            return RESULT_COPIED
        if choice != RESULT_PASTED:
            _restore_now(prev, prev_known, own_seq, text, log)
            if log:
                log.info("Target window changed — insertion cancelled")
            return RESULT_CANCELLED
        time.sleep(0.15)  # let the closed dialog hand the focus back

    kb = Controller()
    time.sleep(PASTE_DELAY_S)
    if paste_method == "shift_insert":
        # Layout-independent: Ctrl+V with a Russian keyboard layout never
        # pastes (measured 2026-11-19); Shift+Insert works on every layout.
        with kb.pressed(Key.shift):
            kb.press(Key.insert)
            kb.release(Key.insert)
    else:  # "ctrl_v"
        with kb.pressed(Key.ctrl):
            kb.press("v")
            kb.release("v")
    time.sleep(AFTER_DELAY_S)
    if log:
        if log_title:
            log.info("Pasted %d chars — window: %s", len(text), target)
        else:
            log.info("Pasted %d chars", len(text))

    if restore_clipboard and prev_known:
        threading.Thread(target=_restore, args=(prev, own_seq, text, log),
                         daemon=True).start()
    return RESULT_PASTED


def _ask_focus_change(confirm_focus, log) -> str:
    if confirm_focus is None:
        return RESULT_COPIED
    try:
        answer = confirm_focus()
    except Exception as e:  # noqa: BLE001 - ctypes clipboard calls fail in many ways; fall back
        if log:
            log.warning("Focus dialog failed: %s", e)
        return RESULT_COPIED
    if answer in (RESULT_PASTED, RESULT_COPIED, RESULT_CANCELLED):
        return answer
    return RESULT_COPIED


def _clipboard_unchanged(own_seq: int, pasted: str) -> bool:
    """True when nobody has written to the clipboard since our own copy."""
    current = clipboard_sequence()
    if own_seq and current:
        return current == own_seq
    try:
        return pyperclip.paste() == pasted
    except Exception:  # noqa: BLE001 - ctypes clipboard calls fail in many ways; fall back
        return False


def _restore_now(prev, prev_known, own_seq, pasted, log):
    if not prev_known:
        return
    if not _clipboard_unchanged(own_seq, pasted):
        if log:
            log.info("Clipboard changed meanwhile — keeping the user's content")
        return
    with contextlib.suppress(Exception):
        pyperclip.copy(prev)


def _restore(prev, own_seq, pasted, log):
    time.sleep(RESTORE_DELAY_S)
    _restore_now(prev, True, own_seq, pasted, log)


def ask_focus_change(cfg=None, text=None) -> str:
    """Ask where the text should go when the target window lost the focus.

    text, when given, enables the "To dictionary" button: it opens the
    "as heard -> as needed" pair dialog over this one, and the choice stays
    open afterwards - the person may still pick where the text goes.
    """
    try:
        import tkinter as tk

        import i18n
    except Exception:  # noqa: BLE001 - ctypes clipboard calls fail in many ways; fall back
        return RESULT_COPIED

    def tr(key, **kw):
        try:
            return i18n.t(cfg, key, **kw)
        except Exception:  # noqa: BLE001 - fall back to the key itself
            return key

    root = None
    try:
        root = tk.Tk()
        root.title("VibeForma")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        answer = {"value": RESULT_COPIED}

        def choose(value):
            answer["value"] = value
            root.quit()

        def open_pair_dialog():
            # A failed dialog must not block the choice.
            with contextlib.suppress(Exception):
                import dictlearn
                dictlearn.pair_dialog(root, tr, text, cfg)

        body = tk.Frame(root, padx=18, pady=14)
        body.pack()
        tk.Label(body, text=tr("focus.changed"), wraplength=380,
                 justify="left").pack(anchor="w")
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        tk.Button(buttons, text=tr("focus.paste_here"),
                  command=lambda: choose(RESULT_PASTED)).pack(side="left")
        tk.Button(buttons, text=tr("focus.copy"),
                  command=lambda: choose(RESULT_COPIED)).pack(side="left",
                                                             padx=8)
        if text:
            tk.Button(buttons, text=tr("focus.to_dictionary"),
                      command=open_pair_dialog).pack(side="left", padx=(0, 8))
        tk.Button(buttons, text=tr("focus.cancel"),
                  command=lambda: choose(RESULT_CANCELLED)).pack(side="left")
        root.after(int(FOCUS_TIMEOUT_S * 1000),
                   lambda: choose(RESULT_COPIED))
        root.mainloop()
        return answer["value"]
    except Exception:  # noqa: BLE001 - ctypes clipboard calls fail in many ways; fall back
        return RESULT_COPIED
    finally:
        if root is not None:
            with contextlib.suppress(Exception):
                root.destroy()
