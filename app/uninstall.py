"""VibeForma - local uninstaller (stdlib only).

Usage:
  uninstall.cmd                 ask, keep models and config by default
  uninstall.cmd --keep-data     same without the question
  uninstall.cmd --remove-data   remove models, config, history, statistics
                                and logs too
  uninstall.cmd --yes           no confirmation prompt

The uninstaller removes the shortcuts, the per-user "Settings > Apps" entry
and everything the installer and the service created in the install folder.
Without --remove-data the user's data stays - models, config.json, logs,
history and statistics - so a reinstall can reuse them offline. With
--remove-data the folder is emptied and removed, and the installer accepts
the same path as a fresh install.
"""
import argparse
import contextlib
import os
import subprocess
import sys
import tempfile

# What the installer copies into the target, plus the marker it writes.
SERVICE_FILES = (
    "run.cmd", "uninstall.cmd", "uninstall.py", "mcp.cmd",
    "requirements.txt", "requirements-cuda.txt",
    "requirements.in", "requirements-cuda.in",
    "test_ru.wav", ".vibeforma-install.json",
)
# The copied folders, the bytecode cache of the first run and the venv.
SERVICE_DIRS = ("assets", "locales", "vibeforma", "__pycache__", ".venv")
# Runtime state: a stale status.json or location.json points at a service
# that is no longer there, so these go even when the user's data is kept.
RUNTIME_FILES = ("service.lock", "status.json", "location.json")
# User data: kept by default, removed with --remove-data. Each of these is
# written atomically, so a crashed write can leave its .tmp companion.
DATA_FILES = ("config.json", "history.jsonl", "stats.json", "corrections.json")
DATA_DIRS = ("models", "logs", "history_audio")
SERVICE_LNK = "VibeForma.lnk"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeForma"

REMOVE_LINKS_PS = r"""$ErrorActionPreference = "Continue"
$ws = New-Object -ComObject WScript.Shell
$removed = 0
foreach ($folder in @([Environment]::GetFolderPath("Desktop"),
                      [Environment]::GetFolderPath("Startup"),
                      [Environment]::GetFolderPath("Programs"))) {
    $lnk = Join-Path $folder "__LNK__"
    if (Test-Path -LiteralPath $lnk) {
        try {
            $shortcut = $ws.CreateShortcut($lnk)
            if ($shortcut.TargetPath -like "*__TARGET__*") {
                Remove-Item -LiteralPath $lnk -Force
                $removed += 1
            }
        } catch {
            Remove-Item -LiteralPath $lnk -Force -ErrorAction SilentlyContinue
            $removed += 1
        }
    }
}
Write-Output ("SHORTCUTS_REMOVED " + $removed)
"""


def say(message=""):
    print(message, flush=True)


def _canon(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def protected_reason(target):
    """A human-readable reason when the target is a system or profile folder."""
    path = _canon(target)
    drive, _rest = os.path.splitdrive(path)
    if drive and path == _canon(drive + os.sep):
        return "a drive root"
    for variable, label in (("SystemRoot", "the Windows system folder"),
                            ("windir", "the Windows system folder"),
                            ("ProgramFiles", "Program Files"),
                            ("ProgramFiles(x86)", "Program Files (x86)"),
                            ("ProgramData", "ProgramData")):
        value = os.environ.get(variable)
        if value and path.startswith(_canon(value) + os.sep):
            return label
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if path == _canon(profile):
        return "your user profile folder"
    return None


def looks_like_vibeforma(target):
    """Refuse to delete a folder that is not our install."""
    if os.path.isfile(os.path.join(target, ".vibeforma-install.json")):
        return True
    return os.path.isfile(os.path.join(target, "run.cmd")) and os.path.isfile(
        os.path.join(target, "main.py"))


def running_inside_venv(target):
    """True when this process's Python lives in the folder being removed."""
    venv = _canon(os.path.join(target, ".venv"))
    return _canon(sys.executable).startswith(venv + os.sep)


def _with_temp(paths):
    """A path and the .tmp companion its atomic write can leave behind."""
    for path in paths:
        yield path
        yield path + ".tmp"


def removal_plan(target, keep_data):
    """Paths to delete, plus whether the folder itself can go."""
    items = []
    for name in SERVICE_FILES:
        items.append(os.path.join(target, name))
    with contextlib.suppress(OSError):
        for name in os.listdir(target):
            if name.endswith(".py"):
                items.append(os.path.join(target, name))
    for name in SERVICE_DIRS:
        items.append(os.path.join(target, name))
    items.extend(_with_temp(
        os.path.join(target, name) for name in RUNTIME_FILES))
    if not keep_data:
        items.extend(_with_temp(
            os.path.join(target, name) for name in DATA_FILES))
        for name in DATA_DIRS:
            items.append(os.path.join(target, name))
    return items


def write_ps1(body):
    fd, path = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(body)
    return path


def remove_shortcuts(target):
    body = (REMOVE_LINKS_PS.replace("__LNK__", SERVICE_LNK)
            .replace("__TARGET__", target.replace("\\", "\\\\")))
    path = write_ps1(body)
    try:
        result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",  # noqa: S607
             "-File", path], capture_output=True, text=True, timeout=60,
            creationflags=0x08000000)
        return "SHORTCUTS_REMOVED" in (result.stdout or "")
    finally:
        os.remove(path)


def remove_uninstall_entry():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                             winreg.KEY_ALL_ACCESS)
        winreg.CloseKey(key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return True
    except OSError:
        return False


def _is_link(path):
    """A symlink or a junction; os.path.islink does not see junctions."""
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        return is_junction(path)
    try:
        os.readlink(path)
    except (OSError, ValueError):
        return False
    return True


def _inside(base, path):
    base = _canon(base)
    real = _canon(path)
    return real == base or real.startswith(base + os.sep)


def _delete_tree(path):
    """Delete one folder tree without ever following a link out of it.

    shutil.rmtree steps into a junction and deletes files outside the
    install folder; this walk removes a link as a link.
    """
    with os.scandir(path) as entries:
        for entry in entries:
            if _is_link(entry.path):
                if entry.is_dir(follow_symlinks=False):
                    os.rmdir(entry.path)
                else:
                    os.remove(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                _delete_tree(entry.path)
            else:
                os.remove(entry.path)
    os.rmdir(path)


def _delete(path, base):
    if _is_link(path):
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)
        return
    if not _inside(base, path):
        raise OSError(f"it resolves outside {base}")
    if os.path.isdir(path):
        _delete_tree(path)
    else:
        os.remove(path)


def delete_paths(items, base):
    """Delete files and folders; return the ones that stayed and why."""
    failed = []
    for path in items:
        if not os.path.lexists(path):
            continue
        try:
            _delete(path, base)
        except OSError as error:
            failed.append((path, str(error)))
    return failed


def main(argv=None):
    ap = argparse.ArgumentParser(description="VibeForma uninstaller")
    ap.add_argument("--target", default=os.path.dirname(os.path.abspath(__file__)),
                    help="service folder (default: the folder of this script)")
    data = ap.add_mutually_exclusive_group()
    data.add_argument("--keep-data", action="store_true",
                      help="keep models, config.json, logs, history and "
                           "statistics (default)")
    data.add_argument("--remove-data", action="store_true",
                      help="remove models, config.json, logs, history and "
                           "statistics too")
    ap.add_argument("--yes", action="store_true", help="do not ask")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        say("[X] The uninstaller targets Windows 10/11.")
        return 1
    target = os.path.abspath(os.path.expandvars(args.target))
    reason = protected_reason(target)
    if reason:
        say(f"[X] Refusing to uninstall from {target}: it is {reason}.")
        return 1
    if not os.path.exists(target):
        say(f"{target} is not there - nothing to remove.")
        return 0
    if not looks_like_vibeforma(target):
        say(f"[X] {target} does not look like a VibeForma install - "
            "nothing was removed.")
        return 1
    keep = not args.remove_data
    say(f"Uninstall VibeForma from {target}?")
    say("Models, config.json (settings and hotwords), logs, history and "
        "statistics will be kept." if keep
        else "Models, config.json (settings and hotwords), logs, history and "
             "statistics will be removed.")
    if not args.yes:
        try:
            answer = input("Continue? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("Cancelled - nothing was removed.")
            return 0
    if running_inside_venv(target):
        say("[!] This uninstaller runs from the folder's .venv; Windows keeps "
            "its python.exe locked while it runs, so .venv stays. Delete the "
            "folder by hand to finish.")

    say("Removing shortcuts...")
    remove_shortcuts(target)
    say("Removing the Settings > Apps entry...")
    remove_uninstall_entry()
    say("Removing the installed files...")
    failed = delete_paths(removal_plan(target, keep), target)
    for path, error in failed:
        say(f"  [X] {path}: {error}")
    remaining = []
    with contextlib.suppress(OSError):
        remaining = os.listdir(target)
    if not remaining:
        with contextlib.suppress(OSError):
            os.rmdir(target)
    say("Done.")
    data_names = set(DATA_FILES + DATA_DIRS)
    failed_names = {os.path.basename(path) for path, _error in failed}
    kept = sorted(name for name in remaining if keep and name in data_names)
    foreign = sorted(name for name in remaining
                     if name not in data_names and name not in failed_names)
    if kept:
        say("Kept your data: " + ", ".join(kept) + ".")
    if foreign:
        say("Kept (not created by VibeForma): " + ", ".join(foreign) + ".")
    if failed:
        say("Some files could not be removed - exit the service and repeat.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
