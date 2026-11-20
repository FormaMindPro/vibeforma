"""Installer as a transaction: preflight, stop on first error, log, rollback."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import install

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_installer(monkeypatch):
    """Never touch the real old folder or the real Settings > Apps key."""
    monkeypatch.setattr(install, "legacy_detected", lambda path=None: None)
    monkeypatch.setattr(install, "write_uninstall_entry", lambda *a, **k: True)


class FakeRun:
    """Stand-in for subprocess.run: green unless a marker matches a failure."""

    def __init__(self, failures=(), on_call=None):
        self.calls = []
        self.failures = list(failures)
        self.on_call = on_call

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        line = " ".join(str(part) for part in args)
        if self.on_call is not None:
            self.on_call(args, line)
        for marker, code, message in self.failures:
            if marker in line:
                return subprocess.CompletedProcess(args, code, "", message)
        return subprocess.CompletedProcess(args, 0, "", "")


def install_argv(monkeypatch, target, log, *extra):
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "small", "--target", str(target),
        "--no-run", "--log", str(log), *extra])


def flat_calls(fake):
    return [" ".join(str(part) for part in call) for call in fake.calls]


def shortcuts_called(fake):
    return any("powershell" in line for line in flat_calls(fake))


def venv_probe(target):
    """Create the venv marker a successful `python -m venv` would create."""
    vp = target / ".venv" / "Scripts" / "python.exe"
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text("", encoding="ascii")
    return vp


@pytest.mark.parametrize("marker,title", [
    ("-m pip install", "Dependencies (pip)"),
    ("local_dir=dest", "Model small"),
])
@pytest.mark.windows
def test_failure_stops_before_shortcuts_and_launch(
        monkeypatch, tmp_path, capsys, marker, title):
    target, log = tmp_path / "vf", tmp_path / "install.log"
    install_argv(monkeypatch, target, log)
    fake = FakeRun(failures=[(marker, 1, "ERROR: simulated failure")])
    monkeypatch.setattr(install.subprocess, "run", fake)
    launched = []
    monkeypatch.setattr(install.os, "startfile",
                        lambda path: launched.append(path), raising=False)

    assert install.main() == 1

    assert not target.exists()
    assert not shortcuts_called(fake)
    assert launched == []
    text = log.read_text(encoding="utf-8")
    assert f"[step] {title}" in text
    assert "simulated failure" in text
    assert f"Installation stopped at step '{title}'" in text
    assert "partial installation removed" in text
    out = capsys.readouterr().out
    assert str(log) in out


@pytest.mark.windows
def test_failure_cleans_an_existing_empty_folder(monkeypatch, tmp_path):
    target, log = tmp_path / "vf", tmp_path / "install.log"
    target.mkdir()
    install_argv(monkeypatch, target, log)
    fake = FakeRun(failures=[("-m pip install", 1, "boom")])
    monkeypatch.setattr(install.subprocess, "run", fake)

    assert install.main() == 1

    assert target.is_dir()
    assert list(target.iterdir()) == []


@pytest.mark.windows
def test_default_target_does_not_trip_over_its_own_log(monkeypatch, tmp_path):
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    target = local / "VibeForma"
    # The default log path resolves to target\logs\install.log at run time;
    # the target is passed explicitly so the test never writes to the profile.
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "small", "--language", "en", "--no-run",
        "--target", str(target)])
    fake = FakeRun()
    monkeypatch.setattr(install.subprocess, "run", fake)

    assert install.main() == 0

    assert (target / "main.py").is_file()
    log_text = (target / "logs" / "install.log").read_text(encoding="utf-8")
    assert "OK (fresh install)" in log_text


@pytest.mark.windows
def test_protected_target_is_refused_before_any_write(monkeypatch, tmp_path):
    target = tmp_path / "Windows"
    log = tmp_path / "install.log"
    monkeypatch.setenv("windir", str(target))
    install_argv(monkeypatch, target, log)
    fake = FakeRun()
    monkeypatch.setattr(install.subprocess, "run", fake)

    assert install.main() == 1

    assert not target.exists()
    assert fake.calls == []
    text = log.read_text(encoding="utf-8")
    assert "protected" in text
    assert "Preflight failed" in text
    assert "Nothing was written" in text


@pytest.mark.windows
def test_existing_install_is_restored_after_a_failed_step(
        monkeypatch, tmp_path):
    target, log = tmp_path / "vf", tmp_path / "install.log"
    target.mkdir()
    (target / "main.py").write_text("old main", encoding="utf-8")
    (target / "run.cmd").write_text("old run", encoding="utf-8")
    (target / "config.json").write_text(
        '{"model": "large-v3"}', encoding="utf-8")
    install_argv(monkeypatch, target, log)
    fake = FakeRun(failures=[("-m pip install", 1, "pip failed")])
    monkeypatch.setattr(install.subprocess, "run", fake)

    assert install.main() == 1

    assert (target / "main.py").read_text(encoding="utf-8") == "old main"
    assert (target / "run.cmd").read_text(encoding="utf-8") == "old run"
    assert (target / "config.json").read_text(encoding="utf-8") == (
        '{"model": "large-v3"}')
    assert not (target / "audio.py").exists()
    assert not shortcuts_called(fake)
    text = log.read_text(encoding="utf-8")
    assert "previous files restored" in text


@pytest.mark.windows
def test_rerun_after_a_model_failure_resumes_and_finishes(
        monkeypatch, tmp_path):
    target, log = tmp_path / "vf", tmp_path / "install.log"
    target.mkdir()
    (target / "main.py").write_text("old main", encoding="utf-8")
    (target / "run.cmd").write_text("old run", encoding="utf-8")
    (target / "config.json").write_text(
        '{"model": "large-v3"}', encoding="utf-8")
    install_argv(monkeypatch, target, log)
    first = FakeRun(failures=[("local_dir=dest", 1, "no network")],
                    on_call=lambda args, line: (
                        venv_probe(target) if "-m venv" in line else None))
    monkeypatch.setattr(install.subprocess, "run", first)

    assert install.main() == 1
    assert any("-m venv" in line for line in flat_calls(first))

    second = FakeRun()
    monkeypatch.setattr(install.subprocess, "run", second)
    assert install.main() == 0

    assert not any("-m venv" in line for line in flat_calls(second))
    assert shortcuts_called(second)
    config = json.loads(
        (target / "config.json").read_text(encoding="utf-8"))
    assert config["model"] == "large-v3"
    marker = json.loads(
        (target / install.INSTALL_MARKER).read_text(encoding="utf-8"))
    assert marker["complete"] is True
    done = install.installer_text(install.installer_language(), "done")
    assert done in log.read_text(encoding="utf-8")


@pytest.mark.windows
def test_dry_run_plans_without_writing(monkeypatch, tmp_path):
    target, log = tmp_path / "vf", tmp_path / "install.log"
    install_argv(monkeypatch, target, log, "--dry-run")
    fake = FakeRun()
    monkeypatch.setattr(install.subprocess, "run", fake)

    assert install.main() == 0

    assert not target.exists()
    assert fake.calls == []
    text = log.read_text(encoding="utf-8")
    assert "[preflight]" in text
    assert "Plan ready" in text


def test_python_version_gate():
    install.check_python_version((3, 10))
    install.check_python_version((3, 14))
    for version in ((3, 9), (3, 15)):
        with pytest.raises(install.InstallError, match="Python"):
            install.check_python_version(version)


def test_windows_build_gate():
    assert install.check_windows(build=install.WINDOWS_MIN_BUILD)
    with pytest.raises(install.InstallError, match="build"):
        install.check_windows(build=install.WINDOWS_MIN_BUILD - 1)


def test_architecture_gate():
    assert install.check_arch("AMD64") == "amd64"
    assert install.check_arch("ARM64") == "arm64"
    with pytest.raises(install.InstallError, match="architecture"):
        install.check_arch("i686")


def test_disk_gate():
    install.check_disk("C:/vf", "small", available=10 * 1024 ** 3)
    with pytest.raises(install.InstallError, match="free space"):
        install.check_disk("C:/vf", "large-v3", available=1024 ** 3)


def test_target_state(tmp_path):
    target = tmp_path / "vf"
    assert install.target_state(target) == "fresh"
    target.mkdir()
    assert install.target_state(target) == "fresh"
    (target / "main.py").write_text("", encoding="utf-8")
    (target / "run.cmd").write_text("", encoding="utf-8")
    assert install.target_state(target) == "existing"
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(install.InstallError, match="not empty"):
        install.target_state(other)


def test_install_ps1_has_no_main_branch_fallback():
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "refs/heads/main" not in text
    assert "codeload.github.com" not in text
    assert "No released VibeForma build" in text
