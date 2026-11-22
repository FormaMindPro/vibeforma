"""CLI flags, status.json and the external hook."""
import io
import json
import os
import subprocess
import sys
import time

import hook
from config import VERSION
from vibeforma import app as app_mod
from vibeforma import status as status_mod
from vibeforma.service import Service

TEXT = "\u043f\u0440\u0438\u0432\u0435\u0442 hello"   # a data literal in the project language


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def debug(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def exception(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)


def _write_helper(tmp_path, body):
    """An ASCII helper script; body reads stdin or exits as the test needs."""
    helper = tmp_path / "helper.py"
    helper.write_text(body, encoding="ascii")
    return helper


def _command(*parts):
    return " ".join(f'"{part}"' for part in parts)


# -- flags -------------------------------------------------------------
def test_parse_flags_reads_the_known_flags():
    assert app_mod.parse_flags(["--toggle"]) == "--toggle"
    assert app_mod.parse_flags(["--cancel"]) == "--cancel"
    assert app_mod.parse_flags(["--status"]) == "--status"


def test_parse_flags_ignores_unknown_arguments():
    assert app_mod.parse_flags([]) is None
    assert app_mod.parse_flags(["--doctor"]) is None
    assert app_mod.parse_flags(["--verbose", "text"]) is None


def test_parse_flags_last_command_wins():
    assert app_mod.parse_flags(["--toggle", "--cancel"]) == "--cancel"


def test_toggle_and_cancel_ask_the_running_instance():
    asked = []

    def signaler(name):
        asked.append(name)
        return True

    out, err = io.StringIO(), io.StringIO()
    assert app_mod.run_cli("--toggle", signaler=signaler, out=out, err=err) == 0
    assert app_mod.run_cli("--cancel", signaler=signaler, out=out, err=err) == 0
    assert asked == ["Local\\VibeForma.Toggle", "Local\\VibeForma.Cancel"]
    assert err.getvalue() == ""


def test_no_instance_means_exit_code_one():
    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli("--toggle", signaler=lambda _name: False,
                           out=out, err=err)
    assert code == 1
    assert "not running" in err.getvalue()
    assert out.getvalue() == ""


# -- status.json -------------------------------------------------------
def test_status_is_written_and_read(tmp_path):
    path = str(tmp_path / "status.json")
    assert status_mod.write_status(path, "idle", "1.2.3", pid=42, now=1000.0)

    payload = status_mod.read_status(path, now=1000.0)

    assert payload == {"state": "idle", "version": "1.2.3", "pid": 42,
                       "ts": 1000.0}
    assert not os.path.exists(path + ".tmp")


def test_status_defaults_to_this_process(tmp_path):
    path = str(tmp_path / "status.json")
    assert status_mod.write_status(path, "recording", VERSION)

    payload = status_mod.read_status(path)

    assert payload["pid"] == os.getpid()
    assert payload["version"] == VERSION


def test_status_is_fresh_for_sixty_seconds(tmp_path):
    path = str(tmp_path / "status.json")
    status_mod.write_status(path, "idle", now=1000.0)

    assert status_mod.read_status(path, now=1060.0) is not None
    assert status_mod.read_status(path, now=1060.1) is None   # stale
    assert status_mod.read_status(path, now=939.9) is None    # future


def test_status_missing_or_broken_is_none(tmp_path):
    assert status_mod.read_status(str(tmp_path / "absent.json")) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert status_mod.read_status(str(broken)) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text('["idle"]', encoding="utf-8")
    assert status_mod.read_status(str(wrong)) is None


def test_status_write_failure_never_raises(tmp_path):
    assert status_mod.write_status(str(tmp_path), "idle") is False


def test_status_prints_fresh_json(tmp_path):
    path = str(tmp_path / "status.json")
    status_mod.write_status(path, "idle", VERSION, pid=7, now=time.time())
    out, err = io.StringIO(), io.StringIO()

    code = app_mod.run_cli("--status", status_file=path, out=out, err=err)

    assert code == 0
    payload = json.loads(out.getvalue())
    assert payload["state"] == "idle"
    assert payload["pid"] == 7
    assert err.getvalue() == ""


def test_status_is_one_when_missing_or_stale(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert app_mod.run_cli("--status", status_file=str(tmp_path / "no.json"),
                           out=out, err=err) == 1
    assert out.getvalue() == ""

    path = str(tmp_path / "status.json")
    status_mod.write_status(path, "idle", now=time.time() - 61)
    out, err = io.StringIO(), io.StringIO()
    assert app_mod.run_cli("--status", status_file=path, out=out,
                           err=err) == 1
    assert out.getvalue() == ""
    assert "no fresh status" in err.getvalue()


def test_service_publishes_every_state_change(tmp_path):
    path = str(tmp_path / "status.json")
    cfg = {"locale": "en", "dictionary": []}
    svc = Service(cfg, FakeLog(), status_file=path)

    assert status_mod.read_status(path)["state"] == "loading"
    svc._set_state("idle")

    assert status_mod.read_status(path)["state"] == "idle"


# -- hook --------------------------------------------------------------
def test_timeout_is_clamped_between_one_and_thirty():
    assert hook.clamp_timeout(0) == 1.0
    assert hook.clamp_timeout(1) == 1.0
    assert hook.clamp_timeout(30) == 30.0
    assert hook.clamp_timeout(999) == 30.0
    assert hook.clamp_timeout("junk") == 5.0
    assert hook.clamp_timeout(None) == 5.0


def test_command_is_split_without_a_shell():
    assert hook.split_command("") == []
    assert hook.split_command("   ") == []
    assert hook.split_command('"C:\\Program Files\\tool.exe" --flag') == [
        "C:\\Program Files\\tool.exe", "--flag"]
    # Metacharacters stay plain arguments: no shell grammar is applied.
    assert hook.split_command("echo a && echo b") == [
        "echo", "a", "&&", "echo", "b"]


def test_hook_receives_the_text_on_stdin(tmp_path):
    helper = _write_helper(
        tmp_path,
        "import sys\n"
        "open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())\n")
    out_file = tmp_path / "got.txt"
    log = FakeLog()

    ok = hook.run_hook(_command(sys.executable, helper, out_file), TEXT, 10, log)

    assert ok is True
    assert out_file.read_bytes() == TEXT.encode("utf-8")
    assert any("exit code: 0" in line for line in log.lines)
    assert TEXT not in "\n".join(log.lines)


def test_hook_metacharacters_are_not_executed(tmp_path):
    helper = _write_helper(
        tmp_path,
        "import sys\n"
        "open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())\n")
    out_file = tmp_path / "got.txt"
    text = "hello & echo HACKED > hacked.txt | more"
    log = FakeLog()

    assert hook.run_hook(_command(sys.executable, helper, out_file), text, 10,
                         log) is True

    assert out_file.read_bytes() == text.encode("utf-8")
    assert not (tmp_path / "hacked.txt").exists()


def test_hook_nonzero_code_is_a_status_only(tmp_path):
    helper = _write_helper(tmp_path, "import sys\nsys.exit(3)\n")
    log = FakeLog()

    ok = hook.run_hook(_command(sys.executable, helper), TEXT, 10, log)

    assert ok is False
    joined = "\n".join(log.lines)
    assert "exit code: 3" in joined
    assert TEXT not in joined


def test_hook_timeout_kills_and_reports(tmp_path):
    helper = _write_helper(tmp_path, "import time\ntime.sleep(10)\n")
    log = FakeLog()
    started = time.monotonic()

    ok = hook.run_hook(_command(sys.executable, helper), TEXT, 1, log)

    assert ok is False
    assert time.monotonic() - started < 8
    joined = "\n".join(log.lines)
    assert "timed out" in joined
    assert TEXT not in joined


def test_empty_command_switches_the_hook_off():
    log = FakeLog()
    assert hook.run_hook("", TEXT, 5, log) is False
    assert hook.run_hook("   ", TEXT, 5, log) is False
    assert log.lines == []


def test_hook_skipped_while_command_is_empty(monkeypatch):
    cfg = {"locale": "en", "dictionary": [], "hook_command": ""}
    svc = Service(cfg, FakeLog())
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: calls.append((a, kw)))
    svc._run_hook(TEXT)
    assert calls == []
