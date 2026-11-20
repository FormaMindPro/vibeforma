"""VibeForma — local installer (stdlib only; heavy deps go into the venv).

Usage:
  install.cmd                     one-click (double click)
  python install.py               same as install.cmd
  python install.py --model large-v3
  python install.py --dry-run     show the plan without executing
  python install.py --cpu         force CPU (int8) config

Without an explicit --model the installer asks: a Tk choice dialog on an
interactive run, the configured model when config.json already exists,
large-v3-turbo silently on an unattended run.
"""
import argparse
import ctypes
import json
import os
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
LNK_NAME = "VibeForma.lnk"

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
Write-Output ("SHORTCUTS_OK desktop=" + $desktop)
"""


def say(msg=""):
    print(msg, flush=True)


def detect_compute(force_cpu):
    """CUDA if nvidia-smi is present, otherwise CPU (int8)."""
    if force_cpu or shutil.which("nvidia-smi") is None:
        return "cpu", "int8"
    return "cuda", "float16"


def write_ps1(body):
    """PS 5.1 needs a BOM to parse non-ASCII correctly."""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(body)
    return path


def model_description(name):
    """Model blurb from the shipped locale (single source of wording)."""
    try:
        with open(os.path.join(APP_DIR, "locales", "en.json"),
                  encoding="utf-8") as f:
            return json.load(f).get("models.desc.%s" % name, "")
    except Exception:
        return ""


def stdin_tty():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def choose_model():
    """Interactive model pick when no explicit --model was passed.

    Tk dialog first (Install / Cancel — Cancel exits the installer);
    a console menu when a tty is attached but Tk is unavailable;
    the default model silently when no console is attached
    (unattended runs — the updater's reinstall path).
    """
    if not stdin_tty():
        say("   no console attached — using the default model %s" % DEFAULT_MODEL)
        return DEFAULT_MODEL
    try:
        # crisp dialog at Windows scaling >100% (the app does the same in main.py)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        root = None
    if root is not None:
        root.title("VibeForma — choose a model")
        root.configure(bg="#16181D")
        root.resizable(False, False)
        tk.Label(root, text="Choose the model to install",
                 font=("Segoe UI", 11, "bold"), fg="#8FB8E8",
                 bg="#16181D").pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(root, text="It downloads once and runs locally; "
                 "you can switch models later in Settings.",
                 font=("Segoe UI", 9), fg="#7A8290", bg="#16181D",
                 wraplength=370, justify="left").pack(fill="x", padx=16)
        var = tk.StringVar(value=DEFAULT_MODEL)
        for name in MODELS:
            row = tk.Frame(root, bg="#16181D")
            row.pack(fill="x", padx=16, pady=(6, 0))
            tk.Radiobutton(row, text="%s  (~%.1f GB)"
                           % (name, MODEL_GB[name]),
                           value=name, variable=var, font=("Segoe UI", 10),
                           fg="#D5D9E0", bg="#16181D", selectcolor="#22262E",
                           activebackground="#16181D",
                           activeforeground="#D5D9E0",
                           highlightthickness=0,
                           anchor="w").pack(fill="x")
            tk.Label(row, text=model_description(name), font=("Segoe UI", 8),
                     fg="#7A8290", bg="#16181D", wraplength=370,
                     justify="left").pack(fill="x", padx=(26, 0))
        result = {"model": None}

        def _install():
            result["model"] = var.get()
            root.destroy()

        btns = tk.Frame(root, bg="#16181D")
        btns.pack(fill="x", pady=(10, 12))
        tk.Button(btns, text="Install", command=_install, width=10,
                  bg="#2E86DE", fg="white", relief="flat",
                  activebackground="#1B6EC2",
                  activeforeground="white").pack(side="right", padx=(4, 16))
        tk.Button(btns, text="Cancel", command=root.destroy, width=10,
                  bg="#22262E", fg="#D5D9E0", relief="flat",
                  activebackground="#2A2F38",
                  activeforeground="#D5D9E0").pack(side="right")
        root.update_idletasks()
        sw_, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("+%d+%d" % ((sw_ - root.winfo_reqwidth()) // 2,
                                  max(60, (sh_ - root.winfo_reqheight()) // 3)))
        root.mainloop()
        return result["model"]   # None when the dialog is closed or cancelled
    # console fallback (tty, no Tk)
    say("")
    say("Choose the model to install:")
    for i, name in enumerate(MODELS, 1):
        say("  %d. %-16s ~%.1f GB   %s"
            % (i, name, MODEL_GB[name], model_description(name)))
    raw = input("Number [1-%d] (Enter = %s): " % (len(MODELS), DEFAULT_MODEL)).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(MODELS):
        return MODELS[int(raw) - 1]
    return DEFAULT_MODEL


def main():
    ap = argparse.ArgumentParser(description="VibeForma installer")
    ap.add_argument("--model", default="",
                    help="model to install; default: ask (dialog on an "
                         "interactive run, the configured model on an "
                         "existing install, %s unattended)" % DEFAULT_MODEL)
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help="service folder (default: %%LOCALAPPDATA%%\\VibeForma)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan without executing")
    ap.add_argument("--no-run", action="store_true",
                    help="do not launch the service at the end")
    ap.add_argument("--cpu", action="store_true",
                    help="force CPU (int8) even when CUDA is present")
    args = ap.parse_args()

    if sys.platform != "win32":
        say("[X] The installer targets Windows.")
        return 1

    target = os.path.abspath(os.path.expandvars(args.target))
    tdir = target.rstrip("\\") + "\\"
    vp = os.path.join(target, ".venv", "Scripts", "python.exe")
    device, compute = detect_compute(args.cpu)

    model = args.model
    if model and model not in MODELS:
        say("[X] Unknown model %s — available: %s" % (model, ", ".join(MODELS)))
        return 1
    if not model:
        # an existing installation keeps its configured model
        cfg_path = os.path.join(target, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    m = json.load(f).get("model")
                if m in MODELS:
                    model = m
                    say("Existing config found — model %s" % m)
            except (OSError, ValueError):
                pass
    if not model and not args.dry_run:
        model = choose_model()
        if model is None:
            say("Installation cancelled — no model chosen.")
            return 0
    if not model:
        model = DEFAULT_MODEL   # dry-run: plan only, no dialog

    say("VibeForma — installation%s" % ("  (dry-run: plan only)" if args.dry_run else ""))
    say("Target folder: %s" % target)
    say("Model: %s (~%.1f GB)   Device: %s (%s)" % (model, MODEL_GB[model], device, compute))
    say("-" * 62)
    failed = False

    def step(title, plan, action=None):
        nonlocal failed
        say("")
        say("[step] %s" % title)
        if args.dry_run:
            say("   plan: %s" % plan)
            return
        try:
            if action:
                action()
            say("   [OK]")
        except subprocess.CalledProcessError as e:
            say("   [X] command exited with code %s" % e.returncode)
            failed = True
        except Exception as e:
            say("   [X] %s" % e)
            failed = True

    def check_python():
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10+ required, found %s"
                               % sys.version.split()[0])

    def copy_app():
        shutil.copytree(APP_DIR, target, dirs_exist_ok=True)

    def make_venv():
        if os.path.exists(vp):
            say("   .venv already exists — reusing it")
            return
        subprocess.run([sys.executable, "-m", "venv", os.path.join(target, ".venv")],
                       check=True)

    def pip_install():
        subprocess.run([vp, "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", os.path.join(target, "requirements.txt")], check=True)

    def model_step():
        dest = os.path.join(target, "models", model.replace("/", "--"))
        if os.path.exists(os.path.join(dest, "model.bin")):
            say("   model already present in %s" % dest)
            return
        probe = subprocess.run([vp, "-c", PROBE_SNIPPET, model],
                               capture_output=True, text=True)
        if "CACHED" in probe.stdout:
            say("   model already in the shared Hugging Face cache — "
                "the service will load it offline")
            return
        say("   downloading to %s (first-time install, the only step using the network)"
            % dest)
        subprocess.run([vp, "-c", DOWNLOAD_SNIPPET, model, dest], check=True)

    def write_config():
        cfg_path = os.path.join(target, "config.json")
        if os.path.exists(cfg_path):
            say("   config.json already exists — not touching it")
            return
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"model": model, "device": device,
                       "compute_type": compute,
                       "language": "en", "locale": "auto"},
                      f, ensure_ascii=False, indent=2)
        say("   device=%s compute_type=%s language=en locale=auto" % (device, compute))

    def make_shortcuts():
        body = (SHORTCUTS_PS.replace("__TARGET__", tdir).replace("__LNK__", LNK_NAME))
        path = write_ps1(body)
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", path], check=True, capture_output=True, text=True)
        finally:
            os.remove(path)

    def run_service():
        os.startfile(os.path.join(target, "run.cmd"))

    step("Python 3.10+", "check the interpreter version", check_python)
    if not args.dry_run and failed:
        return 1
    step("Copy app/ into the service folder", "copytree app/ → %s" % target, copy_app)
    step("Virtual environment .venv", "python -m venv %s\\.venv" % target, make_venv)
    step("Dependencies (pip)", ".venv pip install -r requirements.txt "
         "(faster-whisper, sounddevice, pynput, pystray, Pillow, pyperclip, CUDA libs)",
         pip_install)
    step("Model %s" % model, "probe the Hugging Face cache; if missing, "
         "download to %s\\models\\%s (~%.1f GB, the only step using the network)"
         % (target, model, MODEL_GB[model]), model_step)
    step("config.json", "write model and device (only if no config exists yet)",
         write_config)
    step("Shortcuts", "desktop + startup: «%s» → run.cmd, icon app/assets/icon.ico"
         % LNK_NAME, make_shortcuts)
    if args.no_run:
        say("")
        say("[step] Launch the service — skipped (--no-run)")
    else:
        step("Launch the service", "start run.cmd (tray + panel)", run_service)

    say("")
    say("-" * 62)
    if failed:
        say("Installation stopped at the step above. Fix the cause and run again.")
        return 1
    if args.dry_run:
        say("Plan ready (dry-run — nothing was executed).")
    else:
        say("Done. Hotkey: Ctrl+Alt+Space (by default). Tray icon + panel are up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
