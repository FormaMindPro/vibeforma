"""T6: CPU profile, Windows language, migration from WhisperDictation, uninstall."""
import json
import sys
from pathlib import Path

import pytest

import install
import uninstall

ROOT = Path(__file__).resolve().parents[1]
REAL_WRITE_ENTRY = install.write_uninstall_entry
REAL_LEGACY_DETECTED = install.legacy_detected


class FakeRun:
    """Green unless a marker matches; creates the venv probe like the real one."""

    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)
        self.target = None

    def __call__(self, args, **kwargs):
        import subprocess
        args = list(args)
        self.calls.append(args)
        line = " ".join(str(part) for part in args)
        if "-m venv" in line and self.target is not None:
            venv_probe(self.target)
        for marker, code in self.failures:
            if marker in line:
                return subprocess.CompletedProcess(args, code, "", "boom")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """Tests must never touch the real old folder, shortcuts or the registry."""
    monkeypatch.setattr(install, "legacy_detected", lambda path=None: None)
    monkeypatch.setattr(install, "remove_legacy_shortcuts",
                        lambda *a, **k: True)
    monkeypatch.setattr(install, "write_uninstall_entry", lambda *a, **k: True)


def install_target(tmp_path):
    """A missing target: the installer creates it and copies app/ itself."""
    return tmp_path / "vf"


def venv_probe(target):
    vp = target / ".venv" / "Scripts" / "python.exe"
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text("", encoding="ascii")


def service_target(tmp_path):
    """A fake installed service folder for the uninstaller."""
    target = tmp_path / "vf"
    target.mkdir()
    for name in ("main.py", "run.cmd", "uninstall.cmd", "uninstall.py",
                 "requirements.txt", "requirements-cuda.txt"):
        (target / name).write_text("x", encoding="utf-8")
    (target / "assets").mkdir()
    return target


def run_install(monkeypatch, target, log, *extra, probe=True):
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--target", str(target), "--no-run",
        "--log", str(log), *extra])
    fake = FakeRun()
    fake.target = target if probe else None
    monkeypatch.setattr(install.subprocess, "run", fake)
    return fake


# -- pure helpers -----------------------------------------------------------

def test_language_from_locale():
    assert install.language_from_locale("ru-RU") == "ru"
    assert install.language_from_locale("de_DE") == "de"
    assert install.language_from_locale("fr-CA") == "fr"
    assert install.language_from_locale("en-US") == "en"
    assert install.language_from_locale("ja-JP") == "ja"
    assert install.language_from_locale("xx-YY") == "en"
    assert install.language_from_locale("") == "en"
    assert install.language_from_locale(None) == "en"
    assert "ru" in install.supported_languages()


def test_default_model_follows_device():
    assert install.default_model("cpu") == "small"
    assert install.default_model("cuda") == install.DEFAULT_MODEL


def test_requirements_split():
    assert install.requirements_file("cpu") == "requirements.txt"
    assert install.requirements_file("cuda") == "requirements-cuda.txt"
    base = (ROOT / "app" / "requirements.txt").read_text(encoding="utf-8")
    cuda = (ROOT / "app" / "requirements-cuda.txt").read_text(encoding="utf-8")
    assert "nvidia-" not in base
    # the CUDA lock is standalone: it carries the base pins and the wheels
    assert "faster-whisper==" in cuda
    assert "nvidia-cublas-cu12" in cuda
    assert "nvidia-cudnn-cu12" in cuda


def test_unattended_setup_defaults(monkeypatch):
    monkeypatch.setattr(install, "stdin_tty", lambda: False)
    choice = install.choose_setup(None, "cpu", "ru")
    assert choice == {"model": "small", "language": "ru"}
    choice = install.choose_setup(None, "cuda", "en")
    assert choice == {"model": install.DEFAULT_MODEL, "language": "en"}


def test_migration_copies_config_and_models(tmp_path):
    legacy = tmp_path / "WhisperDictation"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        json.dumps({"model": "medium", "language": "ru",
                    "hotwords": "VibeForma, \u0444\u043e\u0440\u043c\u0430"}), encoding="utf-8")
    model = legacy / "models" / "medium"
    model.mkdir(parents=True)
    (model / "model.bin").write_text("bin", encoding="ascii")
    target = tmp_path / "vf"
    target.mkdir()

    class Log:
        lines = []

        def write(self, message=""):
            self.lines.append(message)

    log = Log()
    moved = install.migrate_legacy(target, legacy, log, model="small")

    assert moved == {"config": True, "models": 1, "hotwords": False}
    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert cfg["hotwords"] == "VibeForma, \u0444\u043e\u0440\u043c\u0430"
    assert cfg["model"] == "small"      # the chosen model wins
    assert (target / "models" / "medium" / "model.bin").is_file()
    assert legacy.is_dir()              # copied, never deleted
    assert any("copied, not deleted" in line for line in log.lines)


def test_migration_merges_hotwords_into_existing_config(tmp_path):
    legacy = tmp_path / "WhisperDictation"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        json.dumps({"hotwords": "old terms"}), encoding="utf-8")
    target = tmp_path / "vf"
    target.mkdir()
    (target / "config.json").write_text(
        json.dumps({"model": "small", "hotwords": ""}), encoding="utf-8")

    class Log:
        def write(self, message=""):
            pass

    moved = install.migrate_legacy(target, legacy, Log(), model="small")

    assert moved["hotwords"] is True
    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert cfg["hotwords"] == "old terms"
    assert cfg["model"] == "small"


def test_legacy_detected_requires_config_or_models(tmp_path):
    assert REAL_LEGACY_DETECTED(tmp_path) is None
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert REAL_LEGACY_DETECTED(tmp_path) == str(tmp_path)


def test_shortcut_scripts_cover_start_menu_and_legacy():
    assert "Programs" in install.SHORTCUTS_PS
    assert "__LNK__" in install.SHORTCUTS_PS
    assert "pythonw.exe" in install.SHORTCUTS_PS
    assert "main.py" in install.SHORTCUTS_PS
    assert "run.cmd" not in install.SHORTCUTS_PS
    assert "__LEGACY__" in install.LEGACY_LINKS_PS
    assert "GetFolderPath(\"Startup\")" in install.LEGACY_LINKS_PS


def test_choose_target_asks_and_falls_back(tmp_path):
    answer = str(tmp_path / "vf")
    assert install.choose_target(
        ask=lambda q: answer, interactive=True) == install.os.path.abspath(answer)
    assert install.choose_target(
        ask=lambda q: "", interactive=True) == install.DEFAULT_TARGET
    assert install.choose_target(
        ask=lambda q: None, interactive=False) == install.DEFAULT_TARGET


def test_service_command_launches_pythonw_directly():
    cmd = install.service_command(r"C:\VF")
    assert cmd[0].endswith(install.os.path.join(
        ".venv", "Scripts", "pythonw.exe"))
    assert cmd[1].endswith("main.py")


def test_uninstall_entry_values(tmp_path):
    values = install.uninstall_entry_values(str(tmp_path), "9.9.9")
    assert values["DisplayName"] == "VibeForma"
    assert values["DisplayVersion"] == "9.9.9"
    assert values["InstallLocation"] == str(tmp_path)
    assert values["UninstallString"].endswith('uninstall.cmd"')
    assert values["NoModify"] == 1


def test_uninstall_entry_written_with_winreg_fake():
    calls = []

    class FakeKey:
        pass

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1
        REG_DWORD = 4

        @staticmethod
        def CreateKey(root, path):
            calls.append(("create", path))
            return FakeKey()

        @staticmethod
        def SetValueEx(key, name, reserved, kind, value):
            calls.append(("set", name, kind, value))

        @staticmethod
        def CloseKey(key):
            calls.append(("close",))

    class Log:
        def write(self, message=""):
            pass

    assert REAL_WRITE_ENTRY("C:\\vf", Log(), "1.2.3",
                            winreg_mod=FakeWinreg)
    names = [call[1] for call in calls if call[0] == "set"]
    assert "DisplayName" in names and "UninstallString" in names


# -- uninstall --------------------------------------------------------------

def test_uninstall_plan_keeps_user_data(tmp_path):
    items = uninstall.removal_plan(str(tmp_path), keep_data=True)
    names = {Path(item).name for item in items}
    assert "models" not in names and "logs" not in names
    assert "config.json" not in names
    assert {"run.cmd", "uninstall.py", ".venv"} <= names
    removed = uninstall.removal_plan(str(tmp_path), keep_data=False)
    removed_names = {Path(item).name for item in removed}
    assert {"models", "logs", "config.json"} <= removed_names


def test_uninstall_refuses_a_foreign_folder(tmp_path):
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")
    assert not uninstall.looks_like_vibeforma(str(tmp_path))
    plan = uninstall.removal_plan(str(tmp_path), keep_data=False)
    assert all("notes.txt" not in item for item in plan)


def test_uninstall_removes_service_files_and_keeps_models(
        monkeypatch, tmp_path, capsys):
    target = service_target(tmp_path)
    (target / "config.json").write_text('{"hotwords": "mine"}',
                                        encoding="utf-8")
    (target / "models" / "small").mkdir(parents=True)
    (target / "models" / "small" / "model.bin").write_text("b")
    (target / ".venv").mkdir()
    monkeypatch.setattr(uninstall, "remove_shortcuts", lambda t: True)
    monkeypatch.setattr(uninstall, "remove_uninstall_entry", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")

    assert uninstall.main(["--target", str(target), "--keep-data",
                           "--yes"]) == 0

    assert not (target / "run.cmd").exists()
    assert not (target / "uninstall.py").exists()
    assert not (target / ".venv").exists()
    assert (target / "config.json").is_file()
    assert (target / "models" / "small" / "model.bin").is_file()
    assert "kept" in capsys.readouterr().out


def test_uninstall_remove_data_clears_the_folder(
        monkeypatch, tmp_path):
    target = service_target(tmp_path)
    (target / "models").mkdir()
    (target / "logs").mkdir()
    (target / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(uninstall, "remove_shortcuts", lambda t: True)
    monkeypatch.setattr(uninstall, "remove_uninstall_entry", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0
    assert not target.exists()


# -- installer run ----------------------------------------------------------

@pytest.mark.windows
def test_cpu_install_writes_cpu_profile_and_windows_language(
        monkeypatch, tmp_path):
    target = install_target(tmp_path)
    log = tmp_path / "install.log"
    fake = run_install(monkeypatch, target, log, "--model", "small")
    monkeypatch.setattr(install, "detect_compute", lambda cpu: ("cpu", "int8"))
    monkeypatch.setattr(install, "windows_language", lambda: "ru")

    assert install.main() == 0

    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert cfg["device"] == "cpu"
    assert cfg["compute_type"] == "int8"
    assert cfg["language"] == "ru"
    assert any("requirements.txt" in " ".join(map(str, call))
               and "requirements-cuda.txt" not in " ".join(map(str, call))
               for call in fake.calls)


@pytest.mark.windows
def test_cuda_install_uses_cuda_requirements(monkeypatch, tmp_path):
    target = install_target(tmp_path)
    log = tmp_path / "install.log"
    fake = run_install(monkeypatch, target, log, "--model", "large-v3")
    monkeypatch.setattr(install, "detect_compute",
                        lambda cpu: ("cuda", "float16"))
    monkeypatch.setattr(install, "windows_language", lambda: "en")

    assert install.main() == 0
    assert any("requirements-cuda.txt" in " ".join(map(str, call))
               for call in fake.calls)


@pytest.mark.windows
def test_existing_cuda_config_is_fixed_on_a_cpu_machine(
        monkeypatch, tmp_path):
    target = install_target(tmp_path)
    target.mkdir()
    (target / "main.py").write_text("old", encoding="utf-8")
    (target / "run.cmd").write_text("old", encoding="utf-8")
    (target / "config.json").write_text(json.dumps({
        "model": "small", "device": "cuda", "compute_type": "float16",
        "language": "ru"}), encoding="utf-8")
    log = tmp_path / "install.log"
    run_install(monkeypatch, target, log, "--model", "small")
    monkeypatch.setattr(install, "detect_compute", lambda cpu: ("cpu", "int8"))
    monkeypatch.setattr(install, "windows_language", lambda: "ru")

    assert install.main() == 0

    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert cfg["device"] == "cpu"
    assert cfg["compute_type"] == "int8"
    assert cfg["model"] == "small"
    assert cfg["language"] == "ru"


@pytest.mark.windows
def test_migration_step_runs_and_old_shortcuts_are_dropped(
        monkeypatch, tmp_path):
    legacy = tmp_path / "WhisperDictation"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        json.dumps({"hotwords": "my terms"}), encoding="utf-8")
    target = install_target(tmp_path)
    log = tmp_path / "install.log"
    run_install(monkeypatch, target, log, "--model", "small")
    monkeypatch.setattr(install, "legacy_detected", lambda path=None: str(legacy))
    monkeypatch.setattr(install, "detect_compute", lambda cpu: ("cpu", "int8"))
    monkeypatch.setattr(install, "windows_language", lambda: "ru")
    dropped = []
    monkeypatch.setattr(install, "remove_legacy_shortcuts",
                        lambda log_, path=None: dropped.append(path) or True)

    assert install.main() == 0

    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert cfg["hotwords"] == "my terms"
    assert dropped == [str(legacy)]
    assert legacy.is_dir()
    text = log.read_text(encoding="utf-8")
    assert "Old install" in text and "Old shortcuts" in text


@pytest.mark.windows
def test_dry_run_names_the_dependency_profile(monkeypatch, tmp_path):
    target = tmp_path / "vf"
    log = tmp_path / "install.log"
    run_install(monkeypatch, target, log, "--dry-run", "--cpu", probe=False)
    monkeypatch.setattr(install, "windows_language", lambda: "ru")

    assert install.main() == 0
    text = log.read_text(encoding="utf-8")
    assert "requirements.txt" in text
    assert "no CUDA wheels" in text
    assert "language=ru" in text
    assert not target.exists()


def test_run_cmd_and_settings_carry_the_name_and_version():
    run_cmd = (ROOT / "app" / "run.cmd").read_text(encoding="utf-8")
    assert "WhisperDictation" not in run_cmd
    assert 'start "VibeForma"' in run_cmd
    settings = (ROOT / "app" / "settings_window.py").read_text(encoding="utf-8")
    assert "settings.version" in settings
    assert "VERSION" in settings
    for name in ("en", "ru"):
        locale = json.loads(
            (ROOT / "app" / "locales" / f"{name}.json").read_text(encoding="utf-8"))
        assert "{ver}" in locale["settings.version"]


def test_uninstall_cmd_and_py_are_shipped():
    assert (ROOT / "app" / "uninstall.cmd").is_file()
    text = (ROOT / "app" / "uninstall.cmd").read_text(encoding="ascii")
    assert "uninstall.py" in text
    assert "uninstall.py" in install.PRODUCT_FILES
    assert "uninstall.cmd" in install.PRODUCT_FILES
