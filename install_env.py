"""Installer environment: languages, texts, preflight checks, target."""
import ast
import contextlib
import ctypes
import os
import platform
import shutil
import sys

from install_core import (
    APP_DIR,
    DEFAULT_MODEL,
    DEFAULT_MODEL_CPU,
    FREE_SPACE_HEADROOM_GB,
    INSTALL_MARKER,
    MODEL_GB,
    MODELS,
    PYTHON_MAX_EXCLUSIVE,
    PYTHON_MIN,
    SUPPORTED_ARCH,
    WINDOWS_MIN_BUILD,
    InstallError,
    model_description,
    say,
)


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


def stdin_tty():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001 - the probe falls back instead of failing the install
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
    except Exception:  # noqa: BLE001 - the probe falls back instead of failing the install
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


def ask_console(question):
    """Yes/no on a console; a closed console means 'yes' (unattended)."""
    if not stdin_tty():
        return True
    try:
        return input(f"   {question} [Y/n]: ").strip().lower() not in ("n", "no")
    except EOFError:
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
    # a failed run leaves only its log folder behind; the install itself was
    # cleaned up, so the target is reusable and must not block a rerun
    if (len(entries) == 1 and entries[0] == "logs"
            and os.path.isdir(os.path.join(target, "logs"))):
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
