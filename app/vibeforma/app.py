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
from .pipe import PipeUnavailable
from .pipe import request as pipe_request
from .selftest import selftest
from .service import Service
from .status import read_status, status_path
from .updates import start_update_checker
from .util import SingleInstance, setup_logging

CLI_TOGGLE = "--toggle"
CLI_CANCEL = "--cancel"
CLI_STATUS = "--status"
CLI_TRANSCRIBE = "--transcribe"
CLI_TRANSCRIBE_FOLDER = "--transcribe-folder"
CLI_CANCEL_BATCH = "--cancel-batch"
CLI_COMMANDS = (CLI_TOGGLE, CLI_CANCEL, CLI_STATUS, CLI_TRANSCRIBE,
                CLI_TRANSCRIBE_FOLDER, CLI_CANCEL_BATCH)

CLI_HELP_FLAGS = ("--help", "-h")
TRANSCRIBE_FORMATS = ("txt", "json", "srt", "vtt")


class CliArgumentError(ValueError):
    """A CLI call that cannot be run as written; exit code 2."""


def transcribe_help() -> str:
    """The CLI contract, printable without reading a source file."""
    return (
        "VibeForma file transcription\n"
        "\n"
        "Usage:\n"
        "  run.cmd --transcribe <file> [options]\n"
        "  .venv\\Scripts\\python.exe main.py --transcribe <file> [options]\n"
        "  run.cmd --toggle | --cancel | --status\n"
        "\n"
        "Options:\n"
        "  --lang <code>          recognition language, e.g. ru, en (default: config)\n"
        "  --format <name>        txt | json | srt | vtt | all (default: srt)\n"
        "  --out <path>           write the result there instead of beside the file\n"
        "  --json                 machine-readable answer on stdout\n"
        "  --force                overwrite an existing output file\n"
        "  --help                 print this contract\n"
        "\n"
        "The file may be audio or video: mp4, mkv, mov, webm, mp3, wav, m4a.\n"
        "A running service is required; the call goes over the current user's\n"
        "named pipe and never opens a port. Progress goes to stderr, the result\n"
        "to stdout as a file path (or JSON with --json).\n"
        "\n"
        "Exit codes: 0 success, 1 service or job error, 2 bad arguments,\n"
        "3 cancelled. While the service is still loading its model the call\n"
        "answers `the model is loading` and exits 1; `VibeForma is not\n"
        "running` appears only when no channel listens.\n"
    )


def cli_help() -> str:
    """Everything the command line offers, for --help without a command."""
    return (
        "VibeForma " + str(VERSION) + "\n"
        "\n"
        "  run.cmd                      start the panel (pythonw, no console)\n"
        "  run.cmd --selftest           run the machine self-test\n"
        "  run.cmd --doctor             print diagnostics\n"
        "  run.cmd --toggle             start or stop a dictation\n"
        "  run.cmd --cancel             drop the dictation in flight\n"
        "  run.cmd --status             print the running service status as JSON\n"
        "  run.cmd --transcribe-folder <dir>   transcribe every media file in a folder\n"
        "  run.cmd --cancel-batch [<id>]       stop every unfinished batch\n"
        "\n" + transcribe_help() +
        "\n" + transcribe_folder_help() +
        "\nDiscovery: %LOCALAPPDATA%\\VibeForma\\location.json names this\n"
        "launcher, the venv python, the entry file and the pipe.\n")


def transcribe_folder_help() -> str:
    """The folder CLI contract, printable without reading a source file."""
    import file_jobs
    media = ", ".join(ext.lstrip(".") for ext in file_jobs.SUPPORTED_MEDIA)
    return (
        "VibeForma batch folder transcription\n"
        "\n"
        "Usage:\n"
        "  run.cmd --transcribe-folder <dir> [options]\n"
        "  .venv\\Scripts\\python.exe main.py --transcribe-folder <dir> [options]\n"
        "  run.cmd --cancel-batch [<batch-id>] [--json]\n"
        "\n"
        "Options:\n"
        "  --lang <code>          recognition language, e.g. ru, en (default: config)\n"
        "  --format <name>        txt | json | srt | vtt | all (default: srt)\n"
        "  --out <path>           mirror the folder tree under that folder\n"
        "  --recursive            include subfolders (default: top level only)\n"
        "  --json                 machine-readable summary on stdout\n"
        "  --force                overwrite existing outputs\n"
        "  --help                 print this contract\n"
        "\n"
        "Supported files: " + media + ". One file at a time, the same\n"
        "engine and formats as a single file; an existing output is skipped\n"
        "unless --force. A running service is required; the call goes over\n"
        "the current user's named pipe and never opens a port. Progress goes\n"
        "to stderr, the summary to stdout (one JSON object with --json).\n"
        "\n"
        "Exit codes: 0 every file done or skipped, 1 a file failed or the\n"
        "service is unavailable, 2 bad arguments or a refused folder, 3 the\n"
        "batch was cancelled, 4 nothing to transcribe in the folder.\n"
    )


def parse_transcribe_args(argv: Sequence[str]) -> dict[str, Any]:
    """The --transcribe call as a dict; a pure function, no process start.

    Raises CliArgumentError for an unknown option, a missing value, an
    unknown format or a second input file.
    """
    options: dict[str, Any] = {
        "path": "", "language": "", "formats": [], "out": "",
        "json": False, "force": False, "help": False,
    }
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == CLI_TRANSCRIBE:
            pass   # the command token itself is not an option
        elif arg in CLI_HELP_FLAGS:
            options["help"] = True
        elif arg in ("--lang", "--language"):
            index += 1
            if index >= len(args):
                raise CliArgumentError("--lang needs a value")
            options["language"] = args[index].strip()
        elif arg == "--format":
            index += 1
            if index >= len(args):
                raise CliArgumentError("--format needs a value")
            value = args[index].strip().lower()
            if value == "all":
                options["formats"] = list(TRANSCRIBE_FORMATS)
            elif value in TRANSCRIBE_FORMATS:
                options["formats"] = [value]
            else:
                raise CliArgumentError(f"unknown format: {value}")
        elif arg in ("--out", "-o"):
            index += 1
            if index >= len(args):
                raise CliArgumentError("--out needs a value")
            options["out"] = args[index].strip()
        elif arg == "--json":
            options["json"] = True
        elif arg == "--force":
            options["force"] = True
        elif arg.startswith("-") and arg != "-":
            raise CliArgumentError(f"unknown option: {arg}")
        elif not options["path"]:
            options["path"] = arg
        else:
            raise CliArgumentError(f"one input file per call: {arg}")
        index += 1
    if not options["formats"]:
        options["formats"] = ["srt"]
    return options


def run_transcribe(argv: Sequence[str], *, out: TextIO | None = None,
                   err: TextIO | None = None,
                   client: Callable[..., dict[str, Any]] | None = None) -> int:
    """Run --transcribe: 0 success, 1 job error, 2 bad args, 3 cancelled."""
    out = out or sys.stdout
    err = err or sys.stderr

    def say(message: str, *, end: str = "\n", flush: bool = False) -> None:
        """Write to stderr, surviving a closed or undrained pipe.

        A harness may capture stderr as a pipe and never read it until the
        process exits; a closed stream raises too. The answer on stdout must
        still come out, so the write is best-effort.
        """
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            print(message, end=end, file=err, flush=flush)

    try:
        options = parse_transcribe_args(argv)
    except CliArgumentError as e:
        say(f"VibeForma: {e}")
        return 2
    if options["help"]:
        print(transcribe_help(), file=out)
        return 0
    if not options["path"]:
        say("VibeForma: --transcribe needs a file")
        return 2
    last: list[int | None] = [None]

    def on_event(event: dict[str, Any]) -> None:
        if event.get("event") != "progress":
            return
        pct = int(round(float(event.get("progress") or 0.0)))
        pct = max(last[0] if last[0] is not None else 0, min(100, pct))
        if pct == last[0]:
            return
        last[0] = pct
        say(f"\rVibeForma: {pct}%", end="", flush=True)

    send = client or pipe_request
    payload = {
        "command": "transcribe",
        "path": options["path"],
        "language": options["language"],
        "formats": options["formats"],
        "out": options["out"],
        "force": options["force"],
    }
    try:
        answer = send(payload, on_event=on_event)
    except PipeUnavailable:
        say("VibeForma is not running")
        return 1
    except (OSError, ValueError) as e:
        say(f"VibeForma: {e}")
        return 1
    if answer.get("ok") and last[0] != 100:
        last[0] = 100
        say("\rVibeForma: 100%", end="", flush=True)
    if last[0] is not None:
        say("")
    if not answer.get("ok"):
        if answer.get("cancelled"):
            say("VibeForma: cancelled")
            return 3
        if answer.get("loading"):
            say("VibeForma: the model is loading")
            return 1
        if answer.get("code") == 2:
            say(f"VibeForma: {answer.get('error')}")
            return 2
        say(f"VibeForma: {answer.get('error') or 'job failed'}")
        return 1
    if options["json"]:
        print(json.dumps({
            "ok": True,
            "job_id": answer.get("job_id"),
            "out": answer.get("out") or [],
            "segments": answer.get("segments"),
            "duration_s": answer.get("duration_s"),
        }, ensure_ascii=False), file=out)
    else:
        for path in answer.get("out") or []:
            print(path, file=out)
    return 0


def parse_transcribe_folder_args(argv: Sequence[str]) -> dict[str, Any]:
    """The --transcribe-folder call as a dict; a pure function.

    Raises CliArgumentError for an unknown option, a missing value, an
    unknown format or a second folder.
    """
    options: dict[str, Any] = {
        "folder": "", "language": "", "formats": [], "out": "",
        "recursive": False, "json": False, "force": False, "help": False,
    }
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == CLI_TRANSCRIBE_FOLDER:
            pass   # the command token itself is not an option
        elif arg in CLI_HELP_FLAGS:
            options["help"] = True
        elif arg in ("--lang", "--language"):
            index += 1
            if index >= len(args):
                raise CliArgumentError("--lang needs a value")
            options["language"] = args[index].strip()
        elif arg == "--format":
            index += 1
            if index >= len(args):
                raise CliArgumentError("--format needs a value")
            value = args[index].strip().lower()
            if value == "all":
                options["formats"] = list(TRANSCRIBE_FORMATS)
            elif value in TRANSCRIBE_FORMATS:
                options["formats"] = [value]
            else:
                raise CliArgumentError(f"unknown format: {value}")
        elif arg in ("--out", "-o"):
            index += 1
            if index >= len(args):
                raise CliArgumentError("--out needs a value")
            options["out"] = args[index].strip()
        elif arg == "--recursive":
            options["recursive"] = True
        elif arg == "--json":
            options["json"] = True
        elif arg == "--force":
            options["force"] = True
        elif arg.startswith("-") and arg != "-":
            raise CliArgumentError(f"unknown option: {arg}")
        elif not options["folder"]:
            options["folder"] = arg
        else:
            raise CliArgumentError(f"one folder per call: {arg}")
        index += 1
    if not options["formats"]:
        options["formats"] = ["srt"]
    return options


def run_transcribe_folder(argv: Sequence[str], *, out: TextIO | None = None,
                          err: TextIO | None = None,
                          client: Callable[..., dict[str, Any]] | None = None) -> int:
    """Run --transcribe-folder: 0 clean, 1 a failed file, 2 arguments,
    3 cancelled, 4 nothing to transcribe.

    Progress goes to stderr only when the whole percent changes; the JSON
    summary goes to stdout as one object, so the two streams never mix. A
    refusal that happened before the batch existed leaves stdout silent.
    """
    out = out or sys.stdout
    err = err or sys.stderr

    def say(message: str, *, end: str = "\n", flush: bool = False) -> None:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            print(message, end=end, file=err, flush=flush)

    try:
        options = parse_transcribe_folder_args(argv)
    except CliArgumentError as e:
        say(f"VibeForma: {e}")
        return 2
    if options["help"]:
        print(transcribe_folder_help(), file=out)
        return 0
    if not options["folder"]:
        say("VibeForma: --transcribe-folder needs a folder")
        return 2
    last: list[int | None] = [None]

    def on_event(event: dict[str, Any]) -> None:
        if event.get("event") != "progress":
            return
        done = (int(event.get("done") or 0) + int(event.get("skipped") or 0)
                + int(event.get("failed") or 0)
                + int(event.get("cancelled") or 0))
        total = int(event.get("total") or 0)
        pct = int(round(float(event.get("progress") or 0.0)))
        pct = max(last[0] if last[0] is not None else 0, min(100, pct))
        if pct == last[0]:
            return
        last[0] = pct
        say(f"VibeForma: batch {done}/{total} \u2014 {pct}%")

    send = client or pipe_request
    payload = {
        "command": "transcribe_folder",
        "folder": options["folder"],
        "language": options["language"],
        "formats": options["formats"],
        "out": options["out"],
        "recursive": options["recursive"],
        "force": options["force"],
    }
    try:
        answer = send(payload, on_event=on_event)
    except PipeUnavailable:
        say("VibeForma is not running")
        return 1
    except (OSError, ValueError) as e:
        say(f"VibeForma: {e}")
        return 1
    if answer.get("cancelled"):
        summary = answer.get("summary") or {}
        if options["json"]:
            print(json.dumps(summary, ensure_ascii=False), file=out)
        say("VibeForma: cancelled")
        return 3
    if answer.get("event") != "done":
        if answer.get("code") == 4:
            say("VibeForma: nothing to transcribe")
            return 4
        if answer.get("loading"):
            say("VibeForma: the model is loading")
            return 1
        if answer.get("code") == 2:
            say(f"VibeForma: {answer.get('error')}")
            return 2
        say(f"VibeForma: {answer.get('error') or 'batch failed'}")
        return 1
    summary = answer.get("summary") or {}
    failed = [row for row in (summary.get("files") or [])
              if row.get("state") == "error"]
    for row in failed[:20]:
        say(f"VibeForma: failed {row.get('relative')}: {row.get('error')}")
    if len(failed) > 20:
        say(f"VibeForma: ... and {len(failed) - 20} more failures (see --json)")
    done = int(summary.get("done") or 0)
    skipped = int(summary.get("skipped") or 0)
    nfailed = int(summary.get("failed") or 0)
    cancelled = int(summary.get("cancelled") or 0)
    line = f"VibeForma: {done} done, {skipped} skipped, {nfailed} failed"
    if cancelled:
        line += f", {cancelled} cancelled"
    say(line)
    if options["json"]:
        print(json.dumps(summary, ensure_ascii=False), file=out)
    else:
        print(summary.get("out") or summary.get("folder")
              or options["folder"], file=out)
    if cancelled:
        return 3
    if nfailed:
        return 1
    return 0


def parse_cancel_batch_args(argv: Sequence[str]) -> dict[str, Any]:
    """The --cancel-batch call as a dict: an optional id and --json."""
    options: dict[str, Any] = {"batch_id": "", "json": False, "help": False}
    args = list(argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == CLI_CANCEL_BATCH:
            pass
        elif arg in CLI_HELP_FLAGS:
            options["help"] = True
        elif arg == "--json":
            options["json"] = True
        elif arg.startswith("-") and arg != "-":
            raise CliArgumentError(f"unknown option: {arg}")
        elif not options["batch_id"]:
            options["batch_id"] = arg
        else:
            raise CliArgumentError(f"one batch id per call: {arg}")
        index += 1
    return options


def run_cancel_batch(argv: Sequence[str], *, out: TextIO | None = None,
                     err: TextIO | None = None,
                     client: Callable[..., dict[str, Any]] | None = None) -> int:
    """Run --cancel-batch: 0 when a batch was cancelled, 1 otherwise."""
    out = out or sys.stdout
    err = err or sys.stderr

    def say(message: str) -> None:
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            print(message, file=err)

    try:
        options = parse_cancel_batch_args(argv)
    except CliArgumentError as e:
        say(f"VibeForma: {e}")
        return 2
    if options["help"]:
        print(transcribe_folder_help(), file=out)
        return 0
    send = client or pipe_request
    try:
        answer = send({"command": "cancel_batch",
                       "batch_id": options["batch_id"]})
    except PipeUnavailable:
        say("VibeForma is not running")
        return 1
    except (OSError, ValueError) as e:
        say(f"VibeForma: {e}")
        return 1
    count = int(answer.get("cancelled") or 0)
    ok = bool(answer.get("ok"))
    payload = {"ok": ok, "cancelled": count}
    if options["json"]:
        print(json.dumps(payload, ensure_ascii=False), file=out)
    else:
        print(f"VibeForma: {count} batch(es) cancelled", file=out)
    return 0 if ok and count > 0 else 1


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
            out: TextIO | None = None, err: TextIO | None = None,
            argv: Sequence[str] | None = None,
            client: Callable[..., dict[str, Any]] | None = None) -> int:
    """Run one CLI command; 0 on success, 1 when there is nobody to talk to.

    --status prints the running instance's fresh status.json as JSON;
    --toggle and --cancel only signal the running instance, never start one;
    --transcribe asks the running instance over the named pipe and prints the
    result path (or JSON) once the job is done.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    if command == CLI_TRANSCRIBE:
        return run_transcribe(argv or [], out=out, err=err, client=client)
    if command == CLI_TRANSCRIBE_FOLDER:
        return run_transcribe_folder(argv or [], out=out, err=err,
                                     client=client)
    if command == CLI_CANCEL_BATCH:
        return run_cancel_batch(argv or [], out=out, err=err, client=client)
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
    if any(arg in CLI_HELP_FLAGS for arg in sys.argv[1:]):
        print(cli_help())
        sys.exit(0)
    argv = sys.argv[1:]
    command = parse_flags(argv)
    if command:
        sys.exit(run_cli(command, argv=argv))

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
    # the file carries the launcher, python, entry, pipe, formats and limits,
    # never the token.
    api_port = int(cfg.get("http_port") or 8787)
    api_state = "off"
    if cfg.get("http_api"):
        svc.http_server = start_http_api(cfg, svc, log)
        if svc.http_server is not None:
            api_port = int(svc.http_server.server_address[1])
            api_state = "on"
        else:
            api_state = "error"
    # The CLI channel starts before the model load: a caller that finds the
    # service through location.json gets a defined "model is loading" answer
    # instead of "VibeForma is not running". The file names the pipe only
    # once the channel actually listens.
    pipe_ready = svc.start_pipe()
    publish_location(log, port=api_port, api=api_state, cfg=cfg,
                     pipe_started=pipe_ready)

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
