"""Voice actions: a whole phrase runs a command, a model switch or an agent.

The phrase is matched after the dictionary and the snippets; a fired action
never reaches the text field, and a command that fails leaves the insertion
and a status in the log.
"""
import sys
import types
from pathlib import Path

from vibeforma.service import Service

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app" / "vibeforma" / "service.py"

CAPTURE = """\
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())
"""

COUNT_AND_FAIL = """\
import pathlib
import sys

with pathlib.Path(sys.argv[1]).open("a", encoding="ascii") as handle:
    handle.write(sys.stdin.read() + "\\n")
sys.exit(3)
"""


class RecordingLog:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def exception(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def debug(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    def text(self):
        return "\n".join(self.lines)


class FakeRecorder:
    def had_speech(self):
        return True

    def stop(self):
        return object()


class FakeASR:
    def __init__(self, text):
        self._text = text

    def transcribe(self, _buf):
        return self._text


class FakeInsert:
    """Stands in for the Windows clipboard module; records every insertion."""

    RESULT_PASTED = "pasted"
    RESULT_COPIED = "copied"
    RESULT_CANCELLED = "cancelled"

    def __init__(self):
        self.inserted = []

    def insert_text(self, text, **_kwargs):
        self.inserted.append(text)
        return self.RESULT_PASTED


def _write_helper(tmp_path, body):
    """An ASCII helper script; body reads stdin and writes or exits."""
    helper = tmp_path / "helper.py"
    helper.write_text(body, encoding="ascii")
    return helper


def _command(*parts):
    return " ".join(f'"{part}"' for part in parts)


def make_service(monkeypatch, **overrides):
    """A service with no tray, no history and a recording stand-in."""
    cfg = {
        "locale": "en",
        "dictionary": [],
        "snippets": [],
        "actions": [],
        "hook_command": "",
        "hud_privacy": False,
        "notify_text": False,
        "history": False,
        "confirm_focus_change": False,
    }
    cfg.update(overrides)
    svc = Service(cfg, RecordingLog())
    svc._running = True
    svc.stats = None          # never touch the real counters
    fake = FakeInsert()
    module = types.ModuleType("insert")
    module.RESULT_PASTED = FakeInsert.RESULT_PASTED
    module.RESULT_COPIED = FakeInsert.RESULT_COPIED
    module.RESULT_CANCELLED = FakeInsert.RESULT_CANCELLED
    module.insert_text = fake.insert_text
    monkeypatch.setitem(sys.modules, "insert", module)
    return svc, fake


def run_phrase(svc, text):
    svc.asr = FakeASR(text)
    svc.state = "processing"
    svc._transcribe_worker(object(), FakeRecorder())


# -- a command action ------------------------------------------------------
def test_a_fired_command_is_never_inserted(tmp_path, monkeypatch):
    out_file = tmp_path / "got.txt"
    helper = _write_helper(tmp_path, CAPTURE)
    svc, fake = make_service(
        monkeypatch,
        hook_command=_command(sys.executable, helper, out_file),
        actions=[{"trigger": "open the browser", "kind": "command"}])

    run_phrase(svc, "Open the browser.")

    assert fake.inserted == []
    assert svc.live["draft"] == ""
    assert out_file.read_bytes() == b"Open the browser."
    assert "Voice action ran" in svc.log.text()
    assert svc.state == "idle"


def test_a_failed_command_keeps_the_insertion(tmp_path, monkeypatch):
    runs = tmp_path / "runs.txt"
    helper = _write_helper(tmp_path, COUNT_AND_FAIL)
    svc, fake = make_service(
        monkeypatch,
        hook_command=_command(sys.executable, helper, runs),
        actions=[{"trigger": "open the browser", "kind": "command"}])

    run_phrase(svc, "open the browser")

    # The words stay with the person: inserted once, the command ran once.
    assert fake.inserted == ["open the browser"]
    assert len(runs.read_text(encoding="ascii").splitlines()) == 1
    logged = svc.log.text()
    assert "exit code: 3" in logged
    assert "Voice action failed" in logged
    assert svc.state == "idle"


def test_without_a_match_the_dictation_is_inserted(monkeypatch):
    svc, fake = make_service(
        monkeypatch,
        actions=[{"trigger": "open the browser", "kind": "command"}])

    run_phrase(svc, "the weather is fine")

    assert fake.inserted == ["the weather is fine"]
    assert "Voice action" not in svc.log.text()


def test_an_action_without_a_command_keeps_the_dictation(monkeypatch):
    svc, fake = make_service(
        monkeypatch,
        hook_command="",
        actions=[{"trigger": "open the browser", "kind": "command"}])

    run_phrase(svc, "open the browser")

    assert fake.inserted == ["open the browser"]
    assert "has no command" in svc.log.text()


def test_a_command_action_may_name_its_own_command(tmp_path, monkeypatch):
    out_file = tmp_path / "got.txt"
    helper = _write_helper(tmp_path, CAPTURE)
    svc, fake = make_service(
        monkeypatch,
        actions=[{"trigger": "open the browser", "kind": "command",
                  "command": _command(sys.executable, helper, out_file)}])

    run_phrase(svc, "open the browser")

    assert fake.inserted == []
    assert out_file.read_bytes() == b"open the browser"


# -- a model action --------------------------------------------------------
def test_a_model_action_switches_after_the_dictation(monkeypatch):
    svc, fake = make_service(
        monkeypatch,
        actions=[{"trigger": "switch to small", "kind": "model",
                  "model": "small"}])
    asked = []
    monkeypatch.setattr(svc, "set_model", asked.append)

    run_phrase(svc, "switch to small")

    assert asked == ["small"]
    assert fake.inserted == []
    assert svc.live["draft"] == ""


# -- an agent action -------------------------------------------------------
def test_an_agent_action_runs_its_own_command(tmp_path, monkeypatch):
    out_file = tmp_path / "got.txt"
    helper = _write_helper(tmp_path, CAPTURE)
    svc, fake = make_service(
        monkeypatch,
        actions=[{"trigger": "ask the agent", "kind": "agent",
                  "command": _command(sys.executable, helper, out_file)}])

    run_phrase(svc, "ask the agent")

    assert fake.inserted == []
    assert out_file.read_bytes() == b"ask the agent"


# -- pipeline order --------------------------------------------------------
def test_a_snippet_can_stand_in_for_the_trigger(tmp_path, monkeypatch):
    out_file = tmp_path / "got.txt"
    helper = _write_helper(tmp_path, CAPTURE)
    svc, fake = make_service(
        monkeypatch,
        hook_command=_command(sys.executable, helper, out_file),
        snippets=[{"trigger": "send report", "text": "deploy now"}],
        actions=[{"trigger": "deploy now", "kind": "command"}])

    run_phrase(svc, "send report")

    assert fake.inserted == []
    assert out_file.read_bytes() == b"deploy now"


def test_a_fired_action_in_the_panel_shows_no_draft(tmp_path, monkeypatch):
    out_file = tmp_path / "got.txt"
    helper = _write_helper(tmp_path, CAPTURE)
    svc, fake = make_service(
        monkeypatch,
        hook_command=_command(sys.executable, helper, out_file),
        actions=[{"trigger": "open the browser", "kind": "command"}])
    svc._panel_mode = True

    run_phrase(svc, "open the browser")

    assert svc.live["draft"] == ""
    assert fake.inserted == []
    assert out_file.read_bytes() == b"open the browser"


def test_the_match_runs_after_the_snippets_and_before_insertion():
    source = SERVICE.read_text(encoding="utf-8")
    snippet_at = source.index("snippets.apply(")
    action_at = source.index("self._match_action(")
    draft_at = source.index("self._show_draft(")
    insert_at = source.index("insert_mod.insert_text(")
    assert snippet_at < action_at < draft_at < insert_at
