"""Installer logic with a substituted subprocess (no venv, pip or network)."""
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
        "install.py", "--model", "bogus", "--target", str(tmp_path / "vf")])

    assert install.main() == 1
    assert "Unknown model" in capsys.readouterr().out


@pytest.mark.windows
def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    target = tmp_path / "vf"
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--dry-run", "--model", "small", "--target", str(target)])

    assert install.main() == 0
    assert not target.exists()


@pytest.mark.windows
def test_install_uses_the_substituted_subprocess(monkeypatch, tmp_path):
    target = tmp_path / "vf"
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--model", "small", "--target", str(target),
        "--no-run"])

    assert install.main() == 0

    assert (target / "config.json").exists()
    assert (target / "main.py").exists()
    flat = [" ".join(str(part) for part in call)
            for call in calls if isinstance(call, list)]
    assert any("-m pip install" in line for line in flat)
    assert any("-m venv" in line for line in flat)
    assert any("powershell" in line for line in flat)
