"""VibeForma - local uninstaller (stdlib only).

Usage:
  uninstall.cmd                 ask, keep models and config by default
  uninstall.cmd --keep-data     same without the question
  uninstall.cmd --remove-data   remove models, config, hotwords and logs too
  uninstall.cmd --yes           no confirmation prompt

The uninstaller removes the shortcuts, the per-user "Settings > Apps" entry
and the service files. Models and config.json stay unless --remove-data is
passed: they are user data, and a reinstall can reuse them offline.
"""
import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

SERVICE_FILES = (
    "run.cmd", "uninstall.cmd", "uninstall.py",
    "requirements.txt", "requirements-cuda.txt",
    "test_ru.wav", "service.lock",
    ".vibeforma-install.json",
)
SERVICE_DIRS = ("assets", "locales")
DATA_FILES = ("config.json",)
DATA_DIRS = ("models", "logs")
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
    items.append(os.path.join(target, ".venv"))
    if not keep_data:
        for name in DATA_FILES:
            items.append(os.path.join(target, name))
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


def delete_paths(items):
    """Delete files and folders; return the ones that stayed and why."""
    failed = []
    for path in items:
        if not os.path.exists(path):
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as error:
            failed.append((path, str(error)))
    return failed


def main(argv=None):
    ap = argparse.ArgumentParser(description="VibeForma uninstaller")
    ap.add_argument("--target", default=os.path.dirname(os.path.abspath(__file__)),
                    help="service folder (default: the folder of this script)")
    data = ap.add_mutually_exclusive_group()
    data.add_argument("--keep-data", action="store_true",
                      help="keep models and config.json (default)")
    data.add_argument("--remove-data", action="store_true",
                      help="remove models, config.json and logs too")
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
    if not looks_like_vibeforma(target):
        say(f"[X] {target} does not look like a VibeForma install - "
            "nothing was removed.")
        return 1
    keep = not args.remove_data
    say(f"Uninstall VibeForma from {target}?")
    say("Models and config.json will be kept." if keep
        else "Models, config.json, hotwords and logs will be removed.")
    if not args.yes:
        try:
            answer = input("Continue? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("Cancelled - nothing was removed.")
            return 0

    say("Removing shortcuts...")
    remove_shortcuts(target)
    say("Removing the Settings > Apps entry...")
    remove_uninstall_entry()
    say("Removing service files...")
    failed = delete_paths(removal_plan(target, keep))
    for path, error in failed:
        say(f"  [X] {path}: {error}")
    remaining = []
    with contextlib.suppress(OSError):
        remaining = os.listdir(target)
    if not remaining:
        with contextlib.suppress(OSError):
            os.rmdir(target)
    say("Done.")
    if keep:
        say("Models and config.json are still in the folder.")
    if failed:
        say("Some files could not be removed - exit the service and repeat.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
