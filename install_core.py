"""Installer core: constants, errors, logging, subprocess helpers."""
import contextlib
import datetime
import os
import re
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(REPO_DIR, "app")
DEFAULT_TARGET = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "VibeForma")
# The model catalog lives with the app; the installer reads the same file.
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
from model_catalog import DEFAULT_MODEL, DEFAULT_MODEL_CPU, MODEL_GB, MODELS  # noqa: E402,F401 - re-exported
from model_catalog import description as model_description  # noqa: E402,F401

LNKS = ("VibeForma.lnk",)
LEGACY_DIR_NAME = "WhisperDictation"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VibeForma"
UNINSTALL_VALUE_NAME = "VibeForma"

INSTALL_MARKER = ".vibeforma-install.json"
INSTALL_LOG_NAME = "install.log"
WINDOWS_MIN_BUILD = 17763
# The audit names 3.10-3.13, but the shipped install runs on Python 3.14.3
# (faster-whisper 1.2.1, CTranslate2 4.9.0), so 3.14 must stay allowed.
PYTHON_MIN = (3, 10)
PYTHON_MAX_EXCLUSIVE = (3, 15)
SUPPORTED_ARCH = {"amd64", "x86_64", "arm64", "aarch64"}
FREE_SPACE_HEADROOM_GB = 4
PRODUCT_FILES = ("run.cmd", "mcp.cmd", "requirements.txt", "requirements-cuda.txt",
                 "requirements.in", "requirements-cuda.in",
                 "uninstall.cmd", "uninstall.py", "test_ru.wav", "config.json")
PRODUCT_DIRS = ("assets", "locales", "vibeforma")
OWN_DIRS = (".venv", "models", "logs")
LOG_TAIL_LINES = 12

PROBE_SNIPPET = """import sys
from huggingface_hub import snapshot_download
name = sys.argv[1]
overrides = {"large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo"}
repo = name if "/" in name else overrides.get(name, "Systran/faster-whisper-%s" % name)
try:
    snapshot_download(repo, local_files_only=True)
    print("CACHED")
except Exception:
    print("MISS")
"""

DOWNLOAD_SNIPPET = """import os, sys
from huggingface_hub import snapshot_download
name, dest = sys.argv[1], sys.argv[2]
overrides = {"large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo"}
repo = name if "/" in name else overrides.get(name, "Systran/faster-whisper-%s" % name)
snapshot_download(repo, local_dir=dest)
mb = os.path.getsize(os.path.join(dest, "model.bin")) / (1024 * 1024)
print("DOWNLOADED model.bin %.0f MB" % mb)
"""

SHORTCUTS_PS = r"""$ErrorActionPreference = "Stop"
$ws = New-Object -ComObject WScript.Shell
$target = "__TARGET__.venv\Scripts\pythonw.exe"
$pyargs = '"__TARGET__main.py"'
$work = "__TARGET__"
$icon = "__TARGET__assets\icon.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = $ws.CreateShortcut((Join-Path $desktop "__LNK__"))
$lnk.TargetPath = $target
$lnk.Arguments = $pyargs
$lnk.WorkingDirectory = $work
$lnk.IconLocation = $icon
$lnk.Save()
$startup = [Environment]::GetFolderPath("Startup")
$slnk = $ws.CreateShortcut((Join-Path $startup "__LNK__"))
$slnk.TargetPath = $target
$slnk.Arguments = $pyargs
$slnk.WorkingDirectory = $work
$slnk.IconLocation = $icon
$slnk.Save()
$programs = [Environment]::GetFolderPath("Programs")
$mlnk = $ws.CreateShortcut((Join-Path $programs "__LNK__"))
$mlnk.TargetPath = $target
$mlnk.Arguments = $pyargs
$mlnk.WorkingDirectory = $work
$mlnk.IconLocation = $icon
$mlnk.Save()
Write-Output ("SHORTCUTS_OK desktop=" + $desktop + " startmenu=" + $programs)
"""

LEGACY_LINKS_PS = r"""$ErrorActionPreference = "Continue"
$ws = New-Object -ComObject WScript.Shell
$removed = 0
foreach ($folder in @([Environment]::GetFolderPath("Desktop"),
                      [Environment]::GetFolderPath("Startup"),
                      [Environment]::GetFolderPath("Programs"))) {
    Get-ChildItem -Path $folder -Filter *.lnk -ErrorAction SilentlyContinue |
        ForEach-Object {
            try {
                $shortcut = $ws.CreateShortcut($_.FullName)
                if ($shortcut.TargetPath -like "*__LEGACY__*") {
                    Remove-Item -LiteralPath $_.FullName -Force
                    $removed += 1
                }
            } catch { }
        }
}
Write-Output ("LEGACY_LINKS_REMOVED " + $removed)
"""


class InstallError(Exception):
    """A failed check or install step; the message is safe to log."""


class CommandFailed(InstallError):
    """A child process returned a non-zero code."""

    def __init__(self, label, returncode, tail):
        InstallError.__init__(self, f"command exited with code {returncode}")
        self.label = label
        self.returncode = returncode
        self.tail = tail


class StepFailed(InstallError):
    """A named install step failed; the chain stops here."""

    def __init__(self, title, reason):
        InstallError.__init__(self, reason)
        self.title = title
        self.reason = reason


def say(msg=""):
    print(msg, flush=True)


def log_path_default():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VibeForma", "logs", INSTALL_LOG_NAME)


class InstallLog:
    """Console echo plus an append-only install log (no secrets, no audio).

    The file opens on start(), not in __init__: creating it before the
    preflight made a fresh folder look non-empty. Lines written before
    start() are buffered.
    """

    def __init__(self, path=None, persist=True):
        self.path = os.path.abspath(path or log_path_default())
        self.persist = persist
        self._handle = None
        self._started = False
        self._pending = []

    def start(self):
        if not self.persist or self._started:
            return
        self._started = True
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            # held open until close(); the installer closes it before it
            # deletes anything inside the target folder
            self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - owns the handle until close()
        except OSError:
            self._handle = None
        pending, self._pending = self._pending, []
        for message in pending:
            self._emit(message)

    def write(self, message=""):
        say(message)
        if not self.persist:
            return
        if not self._started:
            self._pending.append(message)
            return
        self._emit(message)

    def _emit(self, message):
        if self._handle is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._handle.write(f"[{stamp}] {message}\n")
        self._handle.flush()

    def close(self):
        """Release the handle; safe before start() and on a repeated call.

        A live handle keeps logs/install.log locked on Windows, so a target
        cleanup with rmtree(ignore_errors=True) silently leaves the folder
        behind and the next run no longer sees a fresh target. Writes made
        while the log is closed are buffered and flushed when it starts again.
        """
        handle, self._handle = self._handle, None
        self._started = False
        if handle is None:
            return
        with contextlib.suppress(OSError):
            handle.flush()
            handle.close()


def app_version():
    """Read VERSION from app/config.py without importing the app."""
    try:
        with open(os.path.join(APP_DIR, "config.py"), encoding="utf-8") as f:
            match = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        if match:
            return match.group(1)
    except OSError:
        return "unknown"
    return "unknown"


def _quote(part):
    text = str(part)
    return f'"{text}"' if " " in text else text


def _tail(text, lines=LOG_TAIL_LINES):
    rows = [row for row in (text or "").strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def run_logged(cmd, log, label=None, **kwargs):
    """Run a child process, log the command and its error tail, check the rc."""
    shown = label or " ".join(_quote(part) for part in cmd)
    log.write(f"   $ {shown}")
    result = subprocess.run(  # noqa: S603 - fixed argument lists, no shell
        cmd, capture_output=True, text=True, **kwargs)
    log.write(f"   rc={result.returncode}")
    if result.returncode != 0:
        tail = _tail(result.stderr or result.stdout)
        if tail:
            log.write("   " + tail.replace("\n", "\n   "))
        raise CommandFailed(shown, result.returncode, tail)
    return result


def step(log, title, plan, action=None, dry_run=False):
    """One logged step; any failure stops the whole chain."""
    log.write("")
    log.write(f"[step] {title}")
    if dry_run:
        log.write(f"   plan: {plan}")
        return
    try:
        action()
    except StepFailed:
        raise
    except CommandFailed as error:
        log.write(f"   [X] {error}")
        raise StepFailed(title, str(error)) from None
    except Exception as error:  # noqa: BLE001 - a step wraps any action failure into StepFailed
        log.write(f"   [X] {error}")
        raise StepFailed(title, str(error)) from None
    log.write("   [OK]")


def write_ps1(body):
    """PS 5.1 needs a BOM to parse non-ASCII correctly."""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(body)
    return path
