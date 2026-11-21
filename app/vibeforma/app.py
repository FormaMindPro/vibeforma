"""Process startup: DPI awareness, single instance, tray, HUD, workers."""
from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading

from config import LOCK_PATH, VERSION, load

from .http_api import publish_location, start_http_api
from .selftest import selftest
from .service import Service
from .updates import start_update_checker
from .util import SingleInstance, setup_logging


def _enable_dpi_awareness() -> None:
    """Native rendering at any Windows scaling (150% default on laptops).

    Without it Tk draws at 96 DPI and Windows stretches the window as a
    bitmap — all text comes out blurry. Must run before the first window.
    """
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            user32.SetProcessDPIAware()


_SHOW_EVENT = "Local\\VibeForma.ShowPanel"
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


def _signal_show_panel() -> bool:
    """A second launch asks the running instance to show its panel."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _SHOW_EVENT)
    if not handle:
        return False
    try:
        kernel32.SetEvent(handle)
    finally:
        kernel32.CloseHandle(handle)
    return True


def _create_show_event() -> int:
    return int(ctypes.windll.kernel32.CreateEventW(
        None, False, False, _SHOW_EVENT))


def _watch_show_event(handle: int, svc: Service) -> None:
    kernel32 = ctypes.windll.kernel32
    while kernel32.WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0:
        svc.request_panel()


def main() -> None:
    if sys.platform == "win32":
        _enable_dpi_awareness()
    if "--doctor" in sys.argv:
        import doctor
        sys.exit(doctor.main())
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    log = setup_logging(console=False)
    try:
        lock = SingleInstance(LOCK_PATH)
    except RuntimeError:
        if _signal_show_panel():
            log.info("Second instance — asked the running one to show the panel")
        else:
            log.info("Second instance not started — one is already running")
        return
    show_event = _create_show_event()
    log.info("=== service starting (pid %d, version %s) ===",
             os.getpid(), VERSION)

    cfg = load()
    svc = Service(cfg, log)
    if show_event:
        threading.Thread(target=_watch_show_event, args=(show_event, svc),
                         daemon=True).start()

    import hotkey as hotkey_mod
    import tray as tray_mod
    tray = tray_mod.Tray(svc, log)
    svc.attach_tray(tray)
    hk = hotkey_mod.Hotkey(cfg["hotkey"], svc.on_hotkey, log)
    svc.attach_hotkey(hk)
    hk.start()

    # A form finds this install through LOCALAPPDATA\VibeForma\location.json;
    # the file carries the config path only, never the token.
    publish_location(log)
    if cfg.get("http_api"):
        svc.http_server = start_http_api(cfg, svc, log)

    start_update_checker(cfg, svc, log)

    svc.load_model_async()
    if cfg.get("hud", True):
        import hud as hud_mod
        # Tk lives in its own daemon thread; every widget call stays there.
        threading.Thread(target=hud_mod.run_hud, args=(svc, log),
                         daemon=True).start()
        import onboarding as onboarding_mod
        if onboarding_mod.needed(cfg):
            svc.open_onboarding()
    try:
        tray.run()  # main thread owns the icon loop (stable on win32)
    finally:
        svc.shutdown()
        tray.stop()
        lock.release()
        if show_event:
            ctypes.windll.kernel32.CloseHandle(show_event)
        log.info("=== service stopped ===")
