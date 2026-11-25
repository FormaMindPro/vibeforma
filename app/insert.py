"""Insert text into the focused field: clipboard + paste, optional restore.

The dictated text is pasted only into the window that was active when the
recording started. If the focus moved, the text is left in the clipboard —
never a silent paste into a foreign window; the caller decides whether to
offer it as a panel draft instead. The previous clipboard is restored only
when the user has not changed it in the meantime (GetClipboardSequenceNumber).
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
UNDO_VK = 0x5A         # VK_Z: layout-independent Ctrl+Z
SELECTION_MAX_CHARS = 400  # a selected term or phrase, never a document
SELECTION_POLL_S = 0.02    # clipboard settle step after the copy keystroke
SELECTION_POLLS = 10       # the copy never blocks a take for more than ~0.2 s

RESULT_PASTED = "pasted"
RESULT_COPIED = "copied"
RESULT_CANCELLED = "cancelled"


def copy_text(value: str):
    """Put text into the clipboard (history actions, restore)."""
    pyperclip.copy(value)


def read_selected_text(log: logging.Logger | None = None) -> str:
    """The selected text of the focused window, read through the clipboard.

    Ctrl+Insert copies under any keyboard layout; Ctrl+C is a character
    shortcut and was measured to fail on a non-Latin layout (2026-11-19).
    The user's clipboard is saved first and put back when the copy left our
    own reading there, so a take never leaves a foreign selection on the
    clipboard. No selection, an app without a copy shortcut or a failed
    platform call all return "" and never raise: this is a prompt hint, not
    a required input. The text is flattened and bounded; the caller decides
    how much of it reaches the engine.
    """
    prev = None
    prev_known = False
    try:
        prev = pyperclip.paste()
        prev_known = True
    except Exception as e:  # noqa: BLE001 - no readable clipboard means no selection
        if log:
            log.debug("Selected text: clipboard unreadable: %s", e)
    before = clipboard_sequence()
    try:
        kb = Controller()
        with kb.pressed(Key.ctrl):
            kb.press(Key.insert)
            kb.release(Key.insert)
    except Exception as e:  # noqa: BLE001 - no keyboard hook means no selection
        if log:
            log.debug("Selected text: copy keystroke failed: %s", e)
        return ""
    changed = False
    for _attempt in range(SELECTION_POLLS):
        if before and clipboard_sequence() != before:
            changed = True
            break
        time.sleep(SELECTION_POLL_S)
    try:
        text = pyperclip.paste() or ""
    except Exception as e:  # noqa: BLE001 - an unreadable clipboard is an empty selection
        if log:
            log.debug("Selected text: clipboard read failed: %s", e)
        return ""
    if before and not changed:
        return ""
    if not text or text == prev:
        return ""
    flat = " ".join(text.split())
    if len(flat) > SELECTION_MAX_CHARS:
        flat = flat[:SELECTION_MAX_CHARS]
        cut = flat.rfind(" ")
        if cut > 0:
            flat = flat[:cut]
    if prev_known:
        try:
            # Only put the previous clipboard back when nothing else has
            # replaced our reading meanwhile.
            if pyperclip.paste() == text:
                pyperclip.copy(prev)
        except Exception as e:  # noqa: BLE001 - a restore failure must not break a take
            if log:
                log.debug("Selected text: clipboard restore failed: %s", e)
    return flat.strip()


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
        title = window_title(get_foreground_hwnd())
        return title[:80] if title else "(untitled)"
    except Exception:  # noqa: BLE001 - the title lookup fails on unusual windows
        return "(unknown)"


def window_title(hwnd) -> str:
    """Title text of one window; "" when the handle cannot be read.

    The window is named by the address captured at recording start, so a
    dead handle or a window on another desktop yields "" and never raises.
    """
    if not hwnd:
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.c_int]
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
        return buf.value.strip()
    except Exception:  # noqa: BLE001 - a foreign or closed window is unnamed
        return ""


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


def window_process_name(hwnd) -> str:
    """Executable name of the process that owns the window; "" if unreadable.

    The "Code.exe"-style basename is what an app profile matches, with or
    without the suffix. Query-limited rights are enough on Windows Vista+;
    a protected or elevated window may refuse and yields "".
    """
    pid = window_process_id(hwnd)
    if not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_wchar_p,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - a closed handle or a protected process
        return ""


def window_identity(hwnd) -> tuple[str, str]:
    """(process name, window title) of one window; ("", "") when unreadable.

    An app profile matches this pair. The address is the one captured at
    recording start, so the profile follows the window the dictation was
    aimed at, never the window that is foreground later.
    """
    if not hwnd:
        return "", ""
    return window_process_name(hwnd), window_title(hwnd)


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
                paste_method: str = "shift_insert",
                exclude_history: bool = True, log_title: bool = False,
                undo_state: dict | None = None) -> str:
    """Paste text into the target window; returns one of the RESULT_* values.

    When the foreground window differs from target_hwnd the text stays in
    the clipboard and RESULT_COPIED is returned — never a paste into a
    foreign window. undo_state, when given, receives the previous clipboard
    ("prev") and whether it was readable ("prev_known").
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
        if log:
            log.info("Target window changed — the text is in the clipboard")
        return RESULT_COPIED

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
