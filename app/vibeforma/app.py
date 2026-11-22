"""Process startup: DPI awareness, single instance, tray, HUD, workers."""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from config import LOCK_PATH, VERSION, load

from .http_api import publish_location, start_http_api
from .selftest import selftest
from .service import Service
from .status import read_status, status_path
from .updates import start_update_checker
from .util import SingleInstance, setup_logging

CLI_TOGGLE = "--toggle"
CLI_CANCEL = "--cancel"
CLI_STATUS = "--status"
CLI_COMMANDS = (CLI_TOGGLE, CLI_CANCEL, CLI_STATUS)


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
_TOGGLE_EVENT = "Local\\VibeForma.Toggle"
_CANCEL_EVENT = "Local\\VibeForma.Cancel"
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


def parse_flags(argv: Sequence[str]) -> str | None:
    """The CLI command named in argv, or None when none is given.

    A pure function: no Tk, no Windows events, no process start — the flags
    are matched by exact name and the last one wins.
    """
    command = None
    for arg in argv:
        if arg in CLI_COMMANDS:
            command = arg
    return command


def hotkey_mode(cfg: dict[str, Any]) -> str:
    """The hotkey mode the config asks for: 'hold' or 'toggle'.

    A pure choice: no pynput, no listener, no read of the live service, so
    a test reads it directly.
    """
    return "hold" if bool(cfg.get("hold_to_talk", False)) else "toggle"


def build_hotkey(cfg: dict[str, Any], svc: Service, log: Any) -> Any:
    """Create the listener the config asks for; startup and settings agree."""
    import hotkey as hotkey_mod
    return hotkey_mod.make_hotkey(
        cfg["hotkey"], hotkey_mode(cfg) == "hold",
        svc.on_hotkey, svc.on_hotkey_press, svc.on_hotkey_release, log)


def _signal_event(name: str) -> bool:
    """Ask the running instance to act; False when its event does not exist."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        kernel32.SetEvent(handle)
    finally:
        kernel32.CloseHandle(handle)
    return True


def _create_event(name: str) -> int:
    return int(ctypes.windll.kernel32.CreateEventW(None, False, False, name))


def _watch_event(handle: int, callback: Callable[[], None]) -> None:
    kernel32 = ctypes.windll.kernel32
    while kernel32.WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0:
        callback()


def run_cli(command: str, *, status_file: str | None = None,
            signaler: Callable[[str], bool] | None = None,
            out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Run one CLI command; 0 on success, 1 when there is nobody to talk to.

    --status prints the running instance's fresh status.json as JSON;
    --toggle and --cancel only signal the running instance, never start one.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    if command == CLI_STATUS:
        path = status_file or status_path()
        payload = read_status(path)
        if payload is None:
            print(f"VibeForma: no fresh status at {path}", file=err)
            return 1
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return 0
    if command not in (CLI_TOGGLE, CLI_CANCEL):
        print(f"VibeForma: unknown command {command}", file=err)
        return 1
    name = _TOGGLE_EVENT if command == CLI_TOGGLE else _CANCEL_EVENT
    send = signaler or _signal_event
    if not send(name):
        print("VibeForma is not running", file=err)
        return 1
    return 0


def main() -> None:
    if sys.platform == "win32":
        _enable_dpi_awareness()
    if "--doctor" in sys.argv:
        import doctor
        sys.exit(doctor.main())
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    command = parse_flags(sys.argv[1:])
    if command:
        sys.exit(run_cli(command))

    log = setup_logging(console=False)
    try:
        lock = SingleInstance(LOCK_PATH)
    except RuntimeError:
        if _signal_event(_SHOW_EVENT):
            log.info("Second instance — asked the running one to show the panel")
        else:
            log.info("Second instance not started — one is already running")
        return
    show_event = _create_event(_SHOW_EVENT)
    toggle_event = _create_event(_TOGGLE_EVENT)
    cancel_event = _create_event(_CANCEL_EVENT)
    log.info("=== service starting (pid %d, version %s) ===",
             os.getpid(), VERSION)

    cfg = load()
    svc = Service(cfg, log, status_file=status_path())
    for handle, callback in ((show_event, svc.request_panel),
                             (toggle_event, svc.on_hotkey),
                             (cancel_event, svc.cancel_recording)):
        if handle:
            threading.Thread(target=_watch_event, args=(handle, callback),
                             daemon=True).start()

    import cancel_hotkey as cancel_mod
    import tray as tray_mod
    tray = tray_mod.Tray(svc, log)
    svc.attach_tray(tray)
    hk = build_hotkey(cfg, svc, log)
    svc.attach_hotkey(hk)
    hk.start()
    watcher = cancel_mod.EscWatcher(svc.cancel_recording, log)
    svc.attach_cancel_watcher(watcher)

    # A form finds this install through LOCALAPPDATA\VibeForma\location.json;
    # the file carries the config path, the actual API port and whether the
    # listener is on, never the token.
    api_port = int(cfg.get("http_port") or 8787)
    api_state = "off"
    if cfg.get("http_api"):
        svc.http_server = start_http_api(cfg, svc, log)
        if svc.http_server is not None:
            api_port = int(svc.http_server.server_address[1])
            api_state = "on"
        else:
            api_state = "error"
    publish_location(log, port=api_port, api=api_state)

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
        for handle in (show_event, toggle_event, cancel_event):
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
        log.info("=== service stopped ===")
