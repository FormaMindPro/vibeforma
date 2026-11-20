"""VibeForma - local installer (stdlib only; heavy deps go into the venv).

Usage:
  install.cmd                     one-click (double click)
  python install.py               same as install.cmd
  python install.py --model large-v3
  python install.py --language ru
  python install.py --dry-run     show the plan without executing
  python install.py --cpu         force CPU (int8): no CUDA wheels, small model

Without an explicit --model the installer asks: a Tk choice dialog (model
and recognition language) on an interactive run, the configured model when
config.json already exists, small on CPU / large-v3-turbo on CUDA silently
on an unattended run. The recognition language defaults to the Windows
locale, en when it has no matching code.

The install is a transaction. The environment is checked before anything is
written; the first failed step stops the chain (no shortcuts, no config, no
launch); a fresh install removes what it created, an existing install is
restored from a backup. Every step, command and error tail goes to
install.log in %LOCALAPPDATA%\\VibeForma\\logs (override with --log); the
console prints that path.
"""
import argparse
import ast
import contextlib
import ctypes
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(REPO_DIR, "app")
DEFAULT_TARGET = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "VibeForma")
MODELS = ("large-v3", "large-v3-turbo", "medium", "small")
MODEL_GB = {"large-v3": 3.1, "large-v3-turbo": 1.6, "medium": 1.5, "small": 0.5}
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_MODEL_CPU = "small"
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
PRODUCT_FILES = ("run.cmd", "requirements.txt", "requirements-cuda.txt",
                 "uninstall.cmd", "uninstall.py", "test_ru.wav", "config.json")
PRODUCT_DIRS = ("assets", "locales")
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
$target = "__TARGET__run.cmd"
$work = "__TARGET__"
$icon = "__TARGET__app\assets\icon.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = $ws.CreateShortcut((Join-Path $desktop "__LNK__"))
$lnk.TargetPath = $target
$lnk.WorkingDirectory = $work
$lnk.IconLocation = $icon
$lnk.Save()
$startup = [Environment]::GetFolderPath("Startup")
$slnk = $ws.CreateShortcut((Join-Path $startup "__LNK__"))
$slnk.TargetPath = $target
$slnk.WorkingDirectory = $work
$slnk.IconLocation = $icon
$slnk.Save()
$programs = [Environment]::GetFolderPath("Programs")
$mlnk = $ws.CreateShortcut((Join-Path $programs "__LNK__"))
$mlnk.TargetPath = $target
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
    """Console echo plus an append-only install log (no secrets, no audio)."""

    def __init__(self, path=None):
        self.path = os.path.abspath(path or log_path_default())
        self._handle = None
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            # the handle lives for the whole install, by design
            self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        except OSError:
            self._handle = None

    def write(self, message=""):
        say(message)
        if self._handle is None:
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._handle.write(f"[{stamp}] {message}\n")
        self._handle.flush()

    def close(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None


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


def supported_languages():
    """The LANGS tuple from app/config.py, read without importing the app."""
    try:
        with open(os.path.join(APP_DIR, "config.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return ()
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "LANGS" for t in node.targets)
                and isinstance(node.value, (ast.Tuple, ast.List))):
            values = []
            for item in node.value.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    values.append(item.value)
            return tuple(values)
    return ()


def language_from_locale(locale):
    """Recognition code from a Windows locale name; en when unsure."""
    if not locale:
        return "en"
    code = locale.replace("_", "-").split("-")[0].strip().lower()
    supported = supported_languages()
    if code in supported:
        return code
    return "en"


def windows_language():
    """The user's Windows locale, e.g. 'ru-RU'; 'en' off Windows or on error."""
    if sys.platform != "win32":
        return "en"
    try:
        kernel32 = ctypes.windll.kernel32
        size = 85
        buffer = ctypes.create_unicode_buffer(size)
        if kernel32.GetUserDefaultLocaleName(buffer, size):
            return language_from_locale(buffer.value)
    except (AttributeError, OSError):
        pass
    return "en"


def default_model(device):
    """CPU machines get the small model; CUDA keeps the product default."""
    return DEFAULT_MODEL_CPU if device == "cpu" else DEFAULT_MODEL


# Installer UI: ru on a Russian Windows, en everywhere else (fallback en).
# Log lines and step titles stay English on purpose - they are diagnostics
# read by maintainers, not by the person installing.
INSTALLER_TEXTS = {
    "en": {
        "title": "VibeForma - setup",
        "heading": "Choose the model to install",
        "hint": ("It downloads once and runs locally; "
                 "you can switch models later in Settings."),
        "cpu_hint": " No CUDA found - the small model is preselected for CPU.",
        "language_label": "Recognition language:",
        "language_hint": ("The model recognizes exactly this language; "
                          "you can switch it later in the tray or Settings."),
        "install": "Install",
        "cancel": "Cancel",
        "console_choose": "Choose the model to install:",
        "console_number": "Number [1-{n}] (Enter = {default}): ",
        "console_language": "Recognition language [en, ru, ...] (Enter = {language}): ",
        "unattended": "   no console attached - using {model} / {language}",
        "cancelled": "Installation cancelled - no model chosen.",
        "done": ("Done. Hotkey: Ctrl+Alt+Space (by default). "
                 "Tray icon + monitoring panel are up."),
    },
    "ru": {
        "title": "VibeForma - установка",
        "heading": "Выберите модель для установки",
        "hint": ("Она скачается один раз и работает локально; "
                 "модель можно сменить позже в настройках."),
        "cpu_hint": " CUDA не найдена - для процессора выбрана модель small.",
        "language_label": "Язык распознавания:",
        "language_hint": ("Модель распознаёт ровно этот язык; "
                          "его можно сменить позже в трее или настройках."),
        "install": "Установить",
        "cancel": "Отмена",
        "console_choose": "Выберите модель для установки:",
        "console_number": "Номер [1-{n}] (Enter = {default}): ",
        "console_language": "Язык распознавания [en, ru, ...] (Enter = {language}): ",
        "unattended": "   консоль не подключена - беру {model} / {language}",
        "cancelled": "Установка отменена - модель не выбрана.",
        "done": ("Готово. Горячая клавиша: Ctrl+Alt+Space (по умолчанию). "
                 "Иконка в трее и панель мониторинга запущены."),
    },
}


def installer_language(locale=None):
    """Installer UI language from a Windows locale; en is the fallback."""
    raw = str(locale if locale is not None else windows_language() or "")
    code = raw.replace("_", "-").split("-")[0].strip().lower()
    return "ru" if code == "ru" else "en"


def installer_text(lang, key, **kw):
    """A user-facing installer string; an unknown key falls back to en."""
    table = INSTALLER_TEXTS.get(lang) or INSTALLER_TEXTS["en"]
    text = table.get(key)
    if text is None:
        text = INSTALLER_TEXTS["en"].get(key, key)
    return text.format(**kw) if kw else text


def legacy_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, LEGACY_DIR_NAME)


def legacy_detected(path=None):
    """The old WhisperDictation folder with a config or models worth moving."""
    path = str(path or legacy_dir())
    if not os.path.isdir(path):
        return None
    if os.path.isfile(os.path.join(path, "config.json")):
        return path
    if os.path.isdir(os.path.join(path, "models")):
        return path
    return None


def migrate_legacy(target, legacy, log, model=None):
    """Copy the old config, hotwords and models into the new install."""
    moved = {"config": False, "models": 0, "hotwords": False}
    old_config = {}
    try:
        with open(os.path.join(legacy, "config.json"), encoding="utf-8") as f:
            old_config = json.load(f)
    except (OSError, ValueError):
        old_config = {}
    new_path = os.path.join(target, "config.json")
    if old_config and not os.path.exists(new_path):
        if model:
            old_config["model"] = model
            with open(new_path, "w", encoding="utf-8") as f:
                json.dump(old_config, f, ensure_ascii=False, indent=2)
        else:
            shutil.copy2(os.path.join(legacy, "config.json"), new_path)
        moved["config"] = True
    elif old_config and os.path.exists(new_path):
        # keep the new file, but do not lose the user's terms
        with contextlib.suppress(OSError, ValueError):
            with open(new_path, encoding="utf-8") as f:
                new_config = json.load(f)
            old_words = str(old_config.get("hotwords") or "").strip()
            if old_words and not str(new_config.get("hotwords") or "").strip():
                new_config["hotwords"] = old_words
                with open(new_path, "w", encoding="utf-8") as f:
                    json.dump(new_config, f, ensure_ascii=False, indent=2)
                moved["hotwords"] = True
    old_models = os.path.join(legacy, "models")
    if os.path.isdir(old_models):
        dest_root = os.path.join(target, "models")
        os.makedirs(dest_root, exist_ok=True)
        for name in sorted(os.listdir(old_models)):
            source = os.path.join(old_models, name)
            destination = os.path.join(dest_root, name)
            if not os.path.isdir(source) or os.path.exists(destination):
                continue
            try:
                shutil.copytree(source, destination)
                moved["models"] += 1
            except OSError as error:
                log.write(f"   [X] model {name} was not copied: {error}")
    if moved["config"]:
        log.write("   config.json moved from the old folder")
    if moved["hotwords"]:
        log.write("   hotwords merged into the existing config.json")
    if moved["models"]:
        log.write(f"   {moved['models']} model folder(s) copied from the old folder")
    log.write("   the old folder was copied, not deleted - remove it after a "
              "successful start")
    return moved


def remove_legacy_shortcuts(log, legacy=None):
    """Drop desktop/startup/start-menu shortcuts that point at the old folder."""
    legacy = legacy or legacy_dir()
    body = LEGACY_LINKS_PS.replace("__LEGACY__", legacy.replace("\\", "\\\\"))
    path = write_ps1(body)
    try:
        result = run_logged(["powershell", "-NoProfile", "-ExecutionPolicy",
                             "Bypass", "-File", path], log)
    finally:
        os.remove(path)
    return "LEGACY_LINKS_REMOVED" in (result.stdout or "")


def uninstall_entry_values(target, version):
    """Per-user Add/Remove Programs values (no admin rights needed)."""
    script = os.path.join(target, "uninstall.cmd")
    return {
        "DisplayName": UNINSTALL_VALUE_NAME,
        "DisplayVersion": version,
        "Publisher": "FormaMindPro",
        "InstallLocation": target,
        "UninstallString": f'"{script}"',
        "QuietUninstallString": f'"{script}" --yes --keep-data',
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": 1,
    }


def write_uninstall_entry(target, log, version=None, winreg_mod=None):
    """Register the product in Settings > Apps (current user)."""
    if winreg_mod is None:
        import winreg as winreg_mod  # noqa: PLC0415 - Windows-only module
    values = uninstall_entry_values(target, version or app_version())
    try:
        key = winreg_mod.CreateKey(winreg_mod.HKEY_CURRENT_USER, UNINSTALL_KEY)
        try:
            for name, value in values.items():
                winreg_mod.SetValueEx(key, name, 0, winreg_mod.REG_SZ
                                      if isinstance(value, str)
                                      else winreg_mod.REG_DWORD, value)
        finally:
            winreg_mod.CloseKey(key)
    except OSError as error:
        log.write(f"   [X] the uninstall entry was not written: {error}")
        return False
    log.write("   Settings > Apps now lists VibeForma")
    return True


def remove_uninstall_entry(winreg_mod=None):
    if winreg_mod is None:
        import winreg as winreg_mod  # noqa: PLC0415 - Windows-only module
    try:
        key = winreg_mod.OpenKey(winreg_mod.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                                 winreg_mod.KEY_ALL_ACCESS)
    except OSError:
        return False
    winreg_mod.CloseKey(key)
    winreg_mod.DeleteKey(winreg_mod.HKEY_CURRENT_USER, UNINSTALL_KEY)
    return True


def detect_compute(force_cpu):
    """CUDA if nvidia-smi is present, otherwise CPU (int8)."""
    if force_cpu or shutil.which("nvidia-smi") is None:
        return "cpu", "int8"
    return "cuda", "float16"


def check_windows(build=None):
    if sys.platform != "win32":
        raise InstallError("this installer targets Windows 10/11")
    if build is None:
        build = sys.getwindowsversion().build
    if build < WINDOWS_MIN_BUILD:
        raise InstallError(
            f"Windows 10 build {WINDOWS_MIN_BUILD} or newer is required "
            f"(found build {build})")
    return build


def check_arch(machine=None):
    name = (machine or platform.machine()).lower()
    if name not in SUPPORTED_ARCH:
        raise InstallError(
            f"unsupported architecture {name} (x64 or ARM64 is required)")
    return name


def check_python_version(info=None):
    version = tuple((info or sys.version_info)[:2])
    if version < PYTHON_MIN or version >= PYTHON_MAX_EXCLUSIVE:
        wanted = (
            f"{PYTHON_MIN[0]}.{PYTHON_MIN[1]}-"
            f"{PYTHON_MAX_EXCLUSIVE[0]}.{PYTHON_MAX_EXCLUSIVE[1] - 1}")
        raise InstallError(
            f"Python {wanted} is required (found {version[0]}.{version[1]})")
    return version


def _canon(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def protected_reason(target):
    """A human-readable reason when the target is a system or profile folder."""
    path = _canon(target)
    drive, _rest = os.path.splitdrive(path)
    if drive and path == _canon(drive + os.sep):
        return "a drive root"
    folders = (
        ("SystemRoot", "the Windows system folder"),
        ("windir", "the Windows system folder"),
        ("ProgramFiles", "Program Files"),
        ("ProgramFiles(x86)", "Program Files (x86)"),
        ("ProgramData", "ProgramData"),
    )
    for variable, label in folders:
        value = os.environ.get(variable)
        if not value:
            continue
        base = _canon(value)
        if path == base or path.startswith(base + os.sep):
            return label
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if path == _canon(profile):
        return "your user profile folder"
    return None


def looks_like_install(target):
    """Old installs are flat (main.py); a release copy may keep app/main.py."""
    for main in ("main.py", os.path.join("app", "main.py")):
        if (os.path.isfile(os.path.join(target, main))
                and os.path.isfile(os.path.join(target, "run.cmd"))):
            return True
    return False


def target_state(target):
    """'fresh' for a missing or empty folder, 'existing' for our install."""
    if not os.path.exists(target):
        return "fresh"
    if not os.path.isdir(target):
        raise InstallError("the target exists and is not a folder")
    try:
        entries = os.listdir(target)
    except OSError as error:
        raise InstallError(f"the target folder cannot be read: {error}") from None
    if not entries:
        return "fresh"
    if os.path.isfile(os.path.join(target, INSTALL_MARKER)):
        return "existing"
    if looks_like_install(target):
        return "existing"
    raise InstallError(
        "the target folder is not empty and does not look like a VibeForma "
        "install; choose an empty folder or the existing install folder")


def _existing_ancestor(path):
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe


def free_bytes(path):
    return shutil.disk_usage(_existing_ancestor(path)).free


def check_disk(target, model, available=None):
    need = int((MODEL_GB[model] + FREE_SPACE_HEADROOM_GB) * 1024 ** 3)
    if available is None:
        try:
            available = free_bytes(target)
        except OSError as error:
            raise InstallError(
                f"cannot read free space for {target}: {error}") from None
    if available < need:
        raise InstallError(
            f"not enough free space: {need / 1024 ** 3:.1f} GB needed, {available / 1024 ** 3:.1f} GB available")
    return available


def check_writable(target):
    probe = os.path.join(target, ".vibeforma-write-test")
    try:
        os.makedirs(target, exist_ok=True)
        with open(probe, "w", encoding="ascii") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.remove(probe)
        raise InstallError(f"the target folder is not writable: {error}") from None


def write_marker(target, model, device, complete):
    path = os.path.join(target, INSTALL_MARKER)
    previous = {}
    try:
        with open(path, encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        previous = {}
    now = datetime.datetime.now().isoformat(timespec="seconds")
    data = {
        "product": "VibeForma",
        "version": app_version(),
        "model": model,
        "device": device,
        "created": previous.get("created", now),
        "installed": now,
        "complete": bool(complete),
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as error:
        raise InstallError(f"cannot write the install marker: {error}") from None
    return path


def product_entries(target):
    """Top-level files and folders that belong to the product."""
    entries = []
    try:
        names = os.listdir(target)
    except OSError:
        return entries
    for name in names:
        path = os.path.join(target, name)
        if os.path.isdir(path):
            if name in PRODUCT_DIRS:
                entries.append(name)
        elif name.endswith(".py") or name in PRODUCT_FILES:
            entries.append(name)
    return sorted(entries)


def backup_target(target, log):
    folder = tempfile.mkdtemp(prefix="vibeforma-install-backup-")
    names = product_entries(target)
    try:
        for name in names:
            source = os.path.join(target, name)
            destination = os.path.join(folder, name)
            if os.path.isdir(source):
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
    except OSError as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise InstallError(f"cannot back up the existing install: {error}") from None
    log.write(f"   backup of {len(names)} product entries in {folder}")
    return folder, names


def restore_target(target, folder, names):
    """Put the backed-up product entries back, drop files a failed run added."""
    for name in product_entries(target):
        if name in names:
            continue
        path = os.path.join(target, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    for name in names:
        source = os.path.join(folder, name)
        destination = os.path.join(target, name)
        if os.path.isdir(destination):
            shutil.rmtree(destination)
        elif os.path.exists(destination):
            os.remove(destination)
        if os.path.isdir(source):
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


def cleanup_fresh(target, created):
    """Remove a failed first install: the folder or everything it put there."""
    if created:
        shutil.rmtree(target, ignore_errors=True)
        return
    for name in product_entries(target) + list(OWN_DIRS) + [INSTALL_MARKER]:
        path = os.path.join(target, name)
        with contextlib.suppress(OSError):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)


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
    result = subprocess.run(  # noqa: S603
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
    except Exception as error:  # noqa: BLE001 - the transaction needs a reason
        log.write(f"   [X] {error}")
        raise StepFailed(title, str(error)) from None
    log.write("   [OK]")


def write_ps1(body):
    """PS 5.1 needs a BOM to parse non-ASCII correctly."""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(body)
    return path


def model_description(name, lang="en"):
    """Model blurb from the shipped locale (single source of wording)."""

    def blurb(code):
        try:
            with open(os.path.join(APP_DIR, "locales", code + ".json"),
                      encoding="utf-8") as f:
                return json.load(f).get(f"models.desc.{name}") or ""
        except Exception:  # noqa: BLE001 - a missing blurb is not fatal
            return ""

    return blurb(lang) or blurb("en") or ""


def stdin_tty():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001 - a closed console is not fatal
        return False


def choose_setup(log=None, device="cuda", language="en"):
    """Interactive model and language pick when no flags were passed.

    Tk dialog first (Install / Cancel - Cancel exits the installer);
    a console menu when a tty is attached but Tk is unavailable;
    the defaults silently when no console is attached
    (unattended runs - the updater's reinstall path).
    """
    write = log.write if log is not None else say
    default = default_model(device)
    ui = installer_language()
    if not stdin_tty():
        write(installer_text(ui, "unattended", model=default, language=language))
        return {"model": default, "language": language}
    # crisp dialog at Windows scaling >100% (the app does the same in main.py)
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:  # noqa: BLE001 - fall back to the console menu
        root = None
    if root is not None:
        root.title(installer_text(ui, "title"))
        root.configure(bg="#16181D")
        root.resizable(False, False)
        tk.Label(root, text=installer_text(ui, "heading"),
                 font=("Segoe UI", 11, "bold"), fg="#8FB8E8",
                 bg="#16181D").pack(fill="x", padx=16, pady=(12, 2))
        hint = installer_text(ui, "hint")
        if device == "cpu":
            hint += installer_text(ui, "cpu_hint")
        tk.Label(root, text=hint,
                 font=("Segoe UI", 9), fg="#7A8290", bg="#16181D",
                 wraplength=370, justify="left").pack(fill="x", padx=16)
        var = tk.StringVar(value=default)
        for name in MODELS:
            row = tk.Frame(root, bg="#16181D")
            row.pack(fill="x", padx=16, pady=(6, 0))
            tk.Radiobutton(row, text=f"{name}  (~{MODEL_GB[name]:.1f} GB)",
                           value=name, variable=var, font=("Segoe UI", 10),
                           fg="#D5D9E0", bg="#16181D", selectcolor="#22262E",
                           activebackground="#16181D",
                           activeforeground="#D5D9E0",
                           highlightthickness=0,
                           anchor="w").pack(fill="x")
            tk.Label(row, text=model_description(name, ui), font=("Segoe UI", 8),
                     fg="#7A8290", bg="#16181D", wraplength=370,
                     justify="left").pack(fill="x", padx=(26, 0))
        codes = supported_languages() or (language,)
        lang_var = tk.StringVar(value=language)
        lang_row = tk.Frame(root, bg="#16181D")
        lang_row.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(lang_row, text=installer_text(ui, "language_label"),
                 font=("Segoe UI", 10), fg="#D5D9E0",
                 bg="#16181D").pack(side="left")
        tk.OptionMenu(lang_row, lang_var, *codes).configure(
            font=("Segoe UI", 10), bg="#22262E", fg="#D5D9E0",
            activebackground="#2A2F38", highlightthickness=0)
        tk.Label(root, text=installer_text(ui, "language_hint"),
                 font=("Segoe UI", 8), fg="#7A8290", bg="#16181D",
                 wraplength=370, justify="left").pack(fill="x", padx=16)
        result = {"model": None, "language": None}

        def _install():
            result["model"] = var.get()
            result["language"] = lang_var.get()
            root.destroy()

        btns = tk.Frame(root, bg="#16181D")
        btns.pack(fill="x", pady=(10, 12))
        tk.Button(btns, text=installer_text(ui, "install"), command=_install,
                  width=10, bg="#2E86DE", fg="white", relief="flat",
                  activebackground="#1B6EC2",
                  activeforeground="white").pack(side="right", padx=(4, 16))
        tk.Button(btns, text=installer_text(ui, "cancel"), command=root.destroy,
                  width=10, bg="#22262E", fg="#D5D9E0", relief="flat",
                  activebackground="#2A2F38",
                  activeforeground="#D5D9E0").pack(side="right")
        root.update_idletasks()
        sw_, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"+{(sw_ - root.winfo_reqwidth()) // 2}+{max(60, (sh_ - root.winfo_reqheight()) // 3)}")
        root.mainloop()
        return result["model"] and result  # None when cancelled
    # console fallback (tty, no Tk)
    write("")
    write(installer_text(ui, "console_choose"))
    for i, name in enumerate(MODELS, 1):
        write(f"  {i}. {name:<16} ~{MODEL_GB[name]:.1f} GB   "
              f"{model_description(name, ui)}")
    raw = input(installer_text(ui, "console_number", n=len(MODELS),
                               default=default)).strip()
    model = MODELS[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(MODELS) else default
    chosen = input(installer_text(ui, "console_language",
                                  language=language)).strip().lower()
    if chosen not in supported_languages():
        chosen = language
    return {"model": model, "language": chosen}


def copy_app(target):
    shutil.copytree(
        APP_DIR, target, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def copy_test_wav(target):
    src = os.path.join(REPO_DIR, "test_ru.wav")
    if not os.path.isfile(src):
        raise InstallError("test_ru.wav is missing from the release")
    shutil.copy2(src, os.path.join(target, "test_ru.wav"))


def make_venv(target, vp, log):
    if os.path.exists(vp):
        log.write("   .venv already exists - reusing it")
        return
    run_logged([sys.executable, "-m", "venv",
                os.path.join(target, ".venv")], log)


def requirements_file(device):
    """Base dependencies, plus the CUDA wheels only on a CUDA machine."""
    return "requirements-cuda.txt" if device == "cuda" else "requirements.txt"


def pip_install(target, vp, device, log):
    name = requirements_file(device)
    log.write(f"   dependencies: {name}")
    run_logged([vp, "-m", "pip", "install", "--disable-pip-version-check",
                "-r", os.path.join(target, name)], log)


def model_step(target, model, vp, log):
    dest = os.path.join(target, "models", model.replace("/", "--"))
    if os.path.exists(os.path.join(dest, "model.bin")):
        log.write(f"   model already present in {dest}")
        return
    probe = run_logged(
        [vp, "-c", PROBE_SNIPPET, model], log,
        label=f"python -c <probe the Hugging Face cache for {model}>")
    if "CACHED" in (probe.stdout or ""):
        log.write("   model already in the shared Hugging Face cache - "
                  "the service will load it offline")
        return
    log.write(f"   downloading to {dest} (first-time install, the only step "
              "using the network)")
    run_logged(
        [vp, "-c", DOWNLOAD_SNIPPET, model, dest], log,
        label=f"python -c <download {model} to {dest}>")


def write_config(target, model, device, compute, language, log, ask=None):
    """Write a fresh config, or fix the device of an existing one.

    A config left with device=cuda on a machine without CUDA is the exact
    trap the audit found: the service would fail to start. On an interactive
    run the change is confirmed; an unattended run (the updater's reinstall)
    applies it and writes the reason to the log.
    """
    path = os.path.join(target, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                cfg = json.load(handle)
        except (OSError, ValueError):
            cfg = {}
        changed = {}
        if cfg.get("device") != device:
            changed["device"] = (cfg.get("device"), device)
        if cfg.get("compute_type") != compute:
            changed["compute_type"] = (cfg.get("compute_type"), compute)
        if changed:
            question = ", ".join(f"{k}: {old} -> {new}"
                                 for k, (old, new) in changed.items())
            if ask is not None and not ask(question):
                log.write(f"   config.json kept as is (detected: {question})")
                return False
            cfg.update({"device": device, "compute_type": compute})
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle, ensure_ascii=False, indent=2)
            log.write(f"   config.json updated ({question})")
        if not cfg.get("language"):
            cfg["language"] = language
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle, ensure_ascii=False, indent=2)
            log.write(f"   config.json: language={language} (it was missing)")
        else:
            log.write("   config.json already exists - model and language kept")
        return True
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"model": model, "device": device,
                       "compute_type": compute,
                       "language": language, "locale": "auto"},
                      handle, ensure_ascii=False, indent=2)
    except OSError as error:
        raise InstallError(f"cannot write config.json: {error}") from None
    log.write(f"   device={device} compute_type={compute} "
              f"language={language} locale=auto")
    return True


def ask_console(question):
    """Yes/no on a console; a closed console means 'yes' (unattended)."""
    if not stdin_tty():
        return True
    try:
        return input(f"   {question} [Y/n]: ").strip().lower() not in ("n", "no")
    except EOFError:
        return True


def make_shortcuts(target, log):
    tdir = target.rstrip("\\") + "\\"
    body = SHORTCUTS_PS.replace("__TARGET__", tdir).replace("__LNK__", LNKS[0])
    path = write_ps1(body)
    try:
        run_logged(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", path], log)
    finally:
        os.remove(path)


def run_service(target):
    os.startfile(os.path.join(target, "run.cmd"))


def run_selftest(target, vp, log):
    result = run_logged([vp, os.path.join(target, "main.py"), "--selftest"],
                        log, cwd=target)
    return result.returncode


def plan(target, model, device, language, dry_run, log):
    """Print the step list; in dry-run nothing is executed."""
    step(log, "Copy app/ into the service folder",
         f"copytree app/ -> {target}", dry_run=dry_run)
    step(log, "Test WAV",
         f"copy test_ru.wav -> {target}\\test_ru.wav (needed by --selftest)", dry_run=dry_run)
    legacy = legacy_detected()
    step(log, "Old install",
         f"copy config, hotwords and models from {legacy} if present; "
         "drop the old shortcuts" if legacy else "no WhisperDictation folder found",
         dry_run=dry_run)
    step(log, "Virtual environment .venv",
         f"python -m venv {target}\\.venv", dry_run=dry_run)
    step(log, "Dependencies (pip)",
         f".venv pip install -r {requirements_file(device)} ("
         + ("base + CUDA wheels" if device == "cuda" else "base only; no CUDA wheels")
         + ")", dry_run=dry_run)
    step(log, f"Model {model}",
         "probe the Hugging Face cache; if missing, download to "
         f"{target}\\models\\{model} (~{MODEL_GB[model]:.1f} GB, the only step using the network)", dry_run=dry_run)
    step(log, "config.json",
         f"write model, device={device}, compute_type for {device}, "
         f"language={language} (fix the device of an existing config)",
         dry_run=dry_run)
    step(log, "Shortcuts",
         f"desktop + startup + Start menu: {LNKS[0]} -> run.cmd, "
         "icon app/assets/icon.ico", dry_run=dry_run)
    step(log, "Settings > Apps",
         "per-user uninstall entry: uninstall.cmd",
         dry_run=dry_run)


def run(args, log):
    version = app_version()
    log.write("VibeForma {} - installation{}".format(version, "  (dry-run: plan only)" if args.dry_run else ""))
    log.write(f"Log: {log.path}")
    if sys.platform != "win32":
        log.write("[X] The installer targets Windows 10/11.")
        return 1

    target = os.path.abspath(os.path.expandvars(args.target))
    device, compute = detect_compute(args.cpu)
    language = (args.language or "").strip().lower() or windows_language()
    if language not in supported_languages():
        log.write("[X] Unknown language {} - available: {}".format(
            language, ", ".join(supported_languages())))
        return 1

    model = args.model
    if model and model not in MODELS:
        log.write("[X] Unknown model {} - available: {}".format(model, ", ".join(MODELS)))
        return 1
    if not model:
        # an existing installation keeps its configured model
        cfg_path = os.path.join(target, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    configured = json.load(f).get("model")
                if configured in MODELS:
                    model = configured
                    log.write(f"Existing config found - model {configured}")
            except (OSError, ValueError):
                pass
    if not model and not args.dry_run:
        choice = choose_setup(log, device, language)
        if choice is None:
            log.write(installer_text(installer_language(), "cancelled"))
            return 0
        model = choice["model"]
        language = choice["language"]
    if not model:
        model = default_model(device)   # dry-run: plan only, no dialog

    log.write(f"Target folder: {target}")
    log.write(f"Model: {model} (~{MODEL_GB[model]:.1f} GB)   "
              f"Device: {device} ({compute})   Language: {language}")
    log.write("-" * 62)

    created = not os.path.exists(target)
    try:
        check_windows()
        check_arch()
        check_python_version()
        reason = protected_reason(target)
        if reason:
            raise InstallError(
                f"the target {target} is protected ({reason}); choose a normal folder "
                "such as the default")
        state = target_state(target)
        check_disk(target, model)
        if not args.dry_run:
            check_writable(target)
    except InstallError as error:
        log.write(f"[X] Preflight failed: {error}")
        log.write("Nothing was written.")
        if created and os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        return 1
    log.write("[preflight] Windows build, architecture, Python, target folder "
              f"and disk: OK ({state} install)")

    if args.dry_run:
        plan(target, model, device, language, True, log)
        log.write("")
        log.write("-" * 62)
        log.write("Plan ready (dry-run - nothing was executed).")
        return 0

    backup = None
    try:
        write_marker(target, model, device, complete=False)
        if state == "existing":
            backup = backup_target(target, log)
            log.write("   existing install detected - rollback is ready")

        step(log, "Copy app/ into the service folder",
             f"copytree app/ -> {target}", lambda: copy_app(target))
        step(log, "Test WAV", "copy test_ru.wav",
             lambda: copy_test_wav(target))
        legacy = legacy_detected()
        if legacy:
            step(log, "Old install",
                 f"copy config, hotwords and models from {legacy}",
                 lambda: migrate_legacy(target, legacy, log, model=model))
        else:
            log.write("")
            log.write("[step] Old install - no WhisperDictation folder found")
        step(log, "Virtual environment .venv", "python -m venv",
             lambda: make_venv(target, os.path.join(
                 target, ".venv", "Scripts", "python.exe"), log))
        vp = os.path.join(target, ".venv", "Scripts", "python.exe")
        step(log, "Dependencies (pip)",
             f"pip install -r {requirements_file(device)}",
             lambda: pip_install(target, vp, device, log))
        step(log, f"Model {model}", "probe cache, otherwise download",
             lambda: model_step(target, model, vp, log))
        step(log, "config.json", "write or fix model, device and language",
             lambda: write_config(target, model, device, compute, language,
                                  log, ask=ask_console))
        step(log, "Shortcuts", "desktop + startup + Start menu",
             lambda: make_shortcuts(target, log))
        if legacy:
            step(log, "Old shortcuts", "drop the WhisperDictation shortcuts",
                 lambda: remove_legacy_shortcuts(log, legacy))
        step(log, "Settings > Apps", "per-user uninstall entry",
             lambda: write_uninstall_entry(target, log, version=version))
        if args.selftest:
            step(log, "Selftest", "main.py --selftest (model, WAV, clipboard)",
                 lambda: run_selftest(target, vp, log))
        if args.no_run:
            log.write("")
            log.write("[step] Launch the service - skipped (--no-run)")
        else:
            step(log, "Launch the service", "start run.cmd (tray + panel)",
                 lambda: run_service(target))
    except StepFailed as error:
        log.write("")
        log.write(f"[X] Installation stopped at step '{error.title}': {error.reason}")
        if backup is not None:
            try:
                restore_target(target, backup[0], backup[1])
                log.write("   previous files restored")
            except OSError as restore_error:
                log.write(f"   [X] rollback failed: {restore_error}")
        else:
            cleanup_fresh(target, created)
            log.write("   partial installation removed")
        log.write(f"Log: {log.path}")
        return 1
    except InstallError as error:
        log.write(f"[X] Installation could not start: {error}")
        if state == "fresh":
            cleanup_fresh(target, created)
        log.write(f"Log: {log.path}")
        return 1
    finally:
        if backup is not None:
            shutil.rmtree(backup[0], ignore_errors=True)

    try:
        write_marker(target, model, device, complete=True)
    except InstallError as error:
        log.write(f"[X] {error}")
        log.write(f"Log: {log.path}")
        return 1
    log.write("")
    log.write("-" * 62)
    log.write(installer_text(installer_language(), "done"))
    log.write(f"Log: {log.path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="VibeForma installer")
    ap.add_argument("--model", default="",
                    help="model to install; default: ask (dialog on an "
                         "interactive run, the configured model on an "
                         f"existing install, {DEFAULT_MODEL} on CUDA and "
                         f"{DEFAULT_MODEL_CPU} on CPU unattended)")
    ap.add_argument("--language", default="",
                    help="recognition language code (default: the Windows "
                         "locale; en when it has no matching code)")
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help="service folder (default: %%LOCALAPPDATA%%\\VibeForma)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan without executing")
    ap.add_argument("--no-run", action="store_true",
                    help="do not launch the service at the end")
    ap.add_argument("--selftest", action="store_true",
                    help="run app/main.py --selftest after a successful install")
    ap.add_argument("--cpu", action="store_true",
                    help="force CPU (int8) even when CUDA is present")
    ap.add_argument("--log", default="",
                    help="install log path (default: "
                         "%%LOCALAPPDATA%%\\VibeForma\\logs\\install.log)")
    args = ap.parse_args()

    log = InstallLog(args.log or None)
    try:
        return run(args, log)
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
