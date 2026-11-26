"""Installer logic with a substituted subprocess (no venv, pip or network)."""
import os
import shutil
import subprocess
import sys

import pytest

import install


def test_detect_compute_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)

    assert install.detect_compute(False) == ("cpu", "int8")


def test_detect_compute_uses_cuda_when_nvidia_smi_exists(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: "C:/nvidia-smi.exe")

    assert install.detect_compute(False) == ("cuda", "float16")


def test_force_cpu_wins(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: "C:/nvidia-smi.exe")

    assert install.detect_compute(True) == ("cpu", "int8")


@pytest.mark.windows
def test_unknown_model_is_rejected(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "bogus", "--target", str(tmp_path / "vf"),
        "--log", str(tmp_path / "install.log")])

    assert install.main() == 1
    assert "Unknown model" in capsys.readouterr().out


@pytest.mark.windows
def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    target = tmp_path / "vf"
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--dry-run", "--model", "small", "--target", str(target),
        "--log", str(tmp_path / "install.log")])

    assert install.main() == 0
    assert not target.exists()


@pytest.mark.windows
def test_dry_run_without_log_creates_no_log_folder(monkeypatch, tmp_path):
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--dry-run", "--model", "small",
        "--target", str(tmp_path / "vf")])

    assert install.main() == 0
    assert not (local / "VibeForma").exists()


@pytest.mark.windows
def test_install_uses_the_substituted_subprocess(monkeypatch, tmp_path):
    target = tmp_path / "vf"
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(install, "write_uninstall_entry", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "small", "--target", str(target),
        "--no-run", "--log", str(tmp_path / "install.log")])

    assert install.main() == 0

    assert (target / "config.json").exists()
    assert (target / "main.py").exists()
    assert (target / "test_ru.wav").exists()
    flat = [" ".join(str(part) for part in call)
            for call in calls if isinstance(call, list)]
    assert any("-m pip install" in line for line in flat)
    assert any("-m venv" in line for line in flat)
    assert any("powershell" in line for line in flat)


@pytest.mark.windows
def test_selftest_flag_runs_the_service_gate(monkeypatch, tmp_path):
    target = tmp_path / "vf"
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(install, "write_uninstall_entry", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "small", "--target", str(target),
        "--no-run", "--selftest", "--log", str(tmp_path / "install.log")])

    assert install.main() == 0

    flat = [" ".join(str(part) for part in call)
            for call in calls if isinstance(call, list)]
    assert any("main.py --selftest" in line for line in flat)


def test_default_log_path_fresh_logs_lives_outside_the_target(tmp_path, monkeypatch):
    """The default log must not sit in the target: a failed run deletes it."""
    local = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    target = tmp_path / "vf"

    default = os.path.abspath(install.log_path_default())

    assert default.endswith(os.path.join("logs", install.INSTALL_LOG_NAME))
    assert default.startswith(os.path.abspath(str(local)))
    assert os.path.commonpath([default, os.path.abspath(str(target))]) \
        != os.path.abspath(str(target))


@pytest.mark.windows
def test_default_log_fresh_logs_is_not_written_into_the_target(
        monkeypatch, tmp_path):
    local = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    target = tmp_path / "vf"
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "small", "--language", "xx",
        "--target", str(target), "--no-run"])

    assert install.main() == 1

    assert not target.exists()
    assert not (target / "logs").exists()
    assert (local / "VibeForma" / "logs" / "install.log").is_file()


def test_log_close_fresh_logs_releases_a_log_inside_the_target(tmp_path):
    """An open handle keeps logs/install.log locked, so rmtree cannot pass."""
    target = tmp_path / "vf"
    log = install.InstallLog(os.path.join(str(target), "logs", "install.log"))
    log.start()
    log.write("written inside the target")

    log.close()
    log.close()          # safe on a repeated call
    unstarted = install.InstallLog(os.path.join(str(tmp_path), "idle.log"))
    unstarted.close()    # safe before start()

    shutil.rmtree(str(target))
    assert not target.exists()


def test_log_close_fresh_logs_keeps_the_lines_written_while_closed(tmp_path):
    path = tmp_path / "install.log"
    log = install.InstallLog(str(path))
    log.start()
    log.write("before the cleanup")
    log.close()
    log.write("buffered during the cleanup")
    log.start()
    log.write("after the cleanup")
    log.close()

    text = path.read_text(encoding="utf-8")
    assert text.index("before the cleanup") < text.index("buffered during the cleanup")
    assert text.index("buffered during the cleanup") < text.index("after the cleanup")
