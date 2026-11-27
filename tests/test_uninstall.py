"""Uninstaller: the installed inventory, --keep-data, and removal safety."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import uninstall
from install_env import target_state

# Every top-level entry a finished install holds: what the installer copies
# from app/, what it adds, and what the first run leaves behind.
INSTALL_FILES = (
    "main.py", "config.json", "run.cmd", "uninstall.cmd", "uninstall.py",
    "mcp.cmd", "requirements.txt", "requirements-cuda.txt",
    "requirements.in", "requirements-cuda.in", "test_ru.wav",
    ".vibeforma-install.json", "service.lock", "status.json",
    "location.json", "history.jsonl", "stats.json", "corrections.json",
)
INSTALL_DIRS = ("assets", "locales", "vibeforma", "models", "logs",
                "history_audio", ".venv", "__pycache__")
# Read once: the windows fixture patches sys.platform for main(), and a
# junction can only be created on the real Windows.
REAL_WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def no_system_side_effects(monkeypatch):
    """Never touch real shortcuts or the registry from a test."""
    monkeypatch.setattr(uninstall, "remove_shortcuts", lambda target: True)
    monkeypatch.setattr(uninstall, "remove_uninstall_entry", lambda: True)


@pytest.fixture
def windows(monkeypatch):
    """main() refuses other platforms; the file work itself is portable."""
    monkeypatch.setattr(uninstall.sys, "platform", "win32")


def service_target(tmp_path):
    """A folder with every top-level entry a finished install holds."""
    target = tmp_path / "VibeForma"
    target.mkdir()
    for name in INSTALL_FILES:
        (target / name).write_text("x", encoding="utf-8")
    for name in INSTALL_DIRS:
        folder = target / name
        folder.mkdir()
        (folder / "payload.bin").write_text("x", encoding="utf-8")
    return target


def junction(link, target):
    """A directory junction, or None when this machine refuses to make one."""
    if not REAL_WINDOWS:
        return None
    result = subprocess.run(  # noqa: S603 - fixed argv, temp dirs only
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True)
    return link if result.returncode == 0 else None


def test_removal_plan_covers_every_installed_entry(tmp_path):
    target = service_target(tmp_path)

    names = {Path(item).name
             for item in uninstall.removal_plan(str(target), keep_data=False)}

    assert set(INSTALL_FILES) <= names
    assert set(INSTALL_DIRS) <= names


def test_removal_plan_covers_every_app_module(tmp_path):
    target = tmp_path / "VibeForma"
    target.mkdir()
    app = Path(__file__).resolve().parents[1] / "app"
    modules = sorted(path.name for path in app.glob("*.py"))
    for name in modules:
        (target / name).write_text("x", encoding="utf-8")

    names = {Path(item).name
             for item in uninstall.removal_plan(str(target), keep_data=False)}

    assert set(modules) <= names
    assert "uninstall.py" in names


def test_removal_plan_keeps_user_data_by_default(tmp_path):
    target = service_target(tmp_path)

    names = {Path(item).name
             for item in uninstall.removal_plan(str(target), keep_data=True)}

    assert not {"config.json", "history.jsonl", "stats.json", "corrections.json",
                "models", "logs", "history_audio"} & names
    assert {"vibeforma", "mcp.cmd", "requirements.in",
            "requirements-cuda.in"} <= names


def test_remove_data_removes_the_folder(windows, tmp_path):
    target = service_target(tmp_path)

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0

    assert not target.exists()


def test_removed_folder_is_a_fresh_install_for_the_installer(windows, tmp_path):
    target = service_target(tmp_path)
    assert target_state(str(target)) == "existing"

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0

    assert target_state(str(target)) == "fresh"


def test_an_empty_folder_is_a_fresh_install(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()

    assert target_state(str(target)) == "fresh"


def test_keep_data_keeps_models_and_config(windows, capsys, tmp_path):
    target = service_target(tmp_path)

    assert uninstall.main(["--target", str(target), "--keep-data",
                           "--yes"]) == 0

    assert (target / "models" / "payload.bin").is_file()
    assert (target / "config.json").is_file()
    assert (target / "history.jsonl").is_file()
    assert (target / "stats.json").is_file()
    assert (target / "corrections.json").is_file()
    assert (target / "logs" / "payload.bin").is_file()
    assert (target / "history_audio" / "payload.bin").is_file()
    assert not (target / "vibeforma").exists()
    assert not (target / "mcp.cmd").exists()
    assert not (target / "requirements.in").exists()
    assert not (target / "requirements-cuda.in").exists()
    assert not (target / ".venv").exists()
    assert not (target / ".vibeforma-install.json").exists()
    assert not (target / "status.json").exists()
    assert not (target / "location.json").exists()
    assert not (target / "service.lock").exists()
    assert "Kept your data" in capsys.readouterr().out


def test_a_second_run_finds_nothing_to_remove(windows, capsys, tmp_path):
    target = service_target(tmp_path)

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0
    assert not target.exists()

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_missing_files_are_not_an_error(windows, tmp_path):
    target = tmp_path / "VibeForma"
    target.mkdir()
    (target / ".vibeforma-install.json").write_text("{}", encoding="utf-8")

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0

    assert not target.exists()


def test_a_file_of_the_user_is_kept_and_named(windows, capsys, tmp_path):
    target = service_target(tmp_path)
    (target / "notes.txt").write_text("mine", encoding="utf-8")

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0

    assert (target / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert not (target / "config.json").exists()
    assert "notes.txt" in capsys.readouterr().out


def test_a_folder_that_is_not_ours_is_refused(windows, capsys, tmp_path):
    target = tmp_path / "documents"
    target.mkdir()
    (target / "notes.txt").write_text("mine", encoding="utf-8")

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 1

    assert (target / "notes.txt").is_file()


def test_delete_paths_refuses_a_path_outside_the_folder(tmp_path):
    target = tmp_path / "VibeForma"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("mine", encoding="utf-8")

    failed = uninstall.delete_paths([str(outside / "precious.txt")],
                                    str(target))

    assert failed
    assert (outside / "precious.txt").is_file()


def test_a_junction_is_removed_without_its_target(tmp_path):
    target = tmp_path / "VibeForma"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("mine", encoding="utf-8")
    link = junction(target / "models", outside)
    if link is None:
        pytest.skip("this machine cannot create a junction")

    assert uninstall.delete_paths([str(link)], str(target)) == []

    assert not os.path.lexists(link)
    assert (outside / "precious.txt").is_file()


def test_a_nested_junction_is_not_followed(tmp_path):
    target = tmp_path / "VibeForma"
    target.mkdir()
    package = target / "vibeforma"
    package.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("mine", encoding="utf-8")
    link = junction(package / "assets", outside)
    if link is None:
        pytest.skip("this machine cannot create a junction")

    assert uninstall.delete_paths([str(package)], str(target)) == []

    assert not os.path.lexists(package)
    assert (outside / "precious.txt").is_file()


def test_a_junction_in_the_plan_is_not_followed(windows, tmp_path):
    target = service_target(tmp_path)
    (target / "models" / "payload.bin").unlink()
    (target / "models").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("mine", encoding="utf-8")
    link = junction(target / "models", outside)
    if link is None:
        pytest.skip("this machine cannot create a junction")

    assert uninstall.main(["--target", str(target), "--remove-data",
                           "--yes"]) == 0

    assert not target.exists()
    assert (outside / "precious.txt").is_file()


def test_running_inside_venv_is_detected(monkeypatch, tmp_path):
    target = tmp_path / "VibeForma"
    venv_python = target / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="ascii")
    monkeypatch.setattr(uninstall.sys, "executable", str(venv_python))

    assert uninstall.running_inside_venv(str(target))

    system_python = tmp_path / "Python" / "python.exe"
    system_python.parent.mkdir()
    system_python.write_text("", encoding="ascii")
    monkeypatch.setattr(uninstall.sys, "executable", str(system_python))

    assert not uninstall.running_inside_venv(str(target))


# The autouse fixture patches uninstall.remove_shortcuts for the tests
# above; bind the real function here so the new ones can generate a body.
from uninstall import remove_shortcuts as real_remove_shortcuts  # noqa: E402


class _NoPowerShell:
    """Stand-in for uninstall.subprocess: answers without running anything."""

    @staticmethod
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "SHORTCUTS_REMOVED 0", "")


def _link_body(monkeypatch, tmp_path, target):
    """The ps1 body remove_shortcuts builds for target; powershell never runs."""
    captured = {}

    def fake_write_ps1(body):
        captured["body"] = body
        script = tmp_path / "remove_links.ps1"
        script.write_text(body, encoding="utf-8")
        return str(script)

    monkeypatch.setattr(uninstall, "write_ps1", fake_write_ps1)
    monkeypatch.setattr(uninstall, "subprocess", _NoPowerShell)
    assert real_remove_shortcuts(target)
    return captured["body"]


def _like_operand(body):
    """The text between the quotes of the -like operand."""
    line = next(ln for ln in body.splitlines() if "-like" in ln)
    marker = '-like "'
    quoted = line[line.index(marker) + len(marker):]
    assert quoted.endswith('") {')  # the operand is still one quoted string
    return quoted[:-len('") {')]


SPECIAL_TARGET = 'C:\\Vibe `a$b"[c]d'


def test_ps_escape_special_target_generates_escaped_literal(monkeypatch, tmp_path):
    body = _link_body(monkeypatch, tmp_path, SPECIAL_TARGET)

    # exact fragment: brackets wrapped for -like, backtick/$/quote escaped
    # for the string, backslashes still doubled, marker untouched
    assert '-like "*C:\\\\Vibe ``a`$b`"[[]c[]]d*"' in body
    assert "SHORTCUTS_REMOVED" in body


def test_ps_escape_special_target_keeps_one_string_literal(monkeypatch, tmp_path):
    operand = _like_operand(_link_body(monkeypatch, tmp_path, SPECIAL_TARGET))

    # every quote, dollar and backtick inside the literal is part of an
    # escape sequence: drop the sequences and none of them is left
    stripped = operand.replace("``", "").replace("`$", "").replace('`"', "")
    assert "`" not in stripped
    assert "$" not in stripped
    assert '"' not in stripped
    # the path stays one literal and its backslashes stay doubled
    assert operand.startswith("*C:\\\\Vibe")


def test_ps_escape_plain_target_body_is_the_previous_body(monkeypatch, tmp_path):
    plain = r"C:\Users\me\VibeForma"

    body = _link_body(monkeypatch, tmp_path, plain)

    expected = (uninstall.REMOVE_LINKS_PS
                .replace("__LNK__", uninstall.SERVICE_LNK)
                .replace("__TARGET__", plain.replace("\\", "\\\\")))
    assert body == expected
