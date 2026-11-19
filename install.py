"""VibeForma — local installer (stdlib only; heavy deps go into the venv).

Usage:
  install.cmd                     one-click (double click)
  python install.py               same as install.cmd
  python install.py --model large-v3
  python install.py --dry-run     show the plan without executing
  python install.py --cpu         force CPU (int8) config
"""
import argparse
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


def main():
    ap = argparse.ArgumentParser(description="VibeForma installer")
    ap.add_argument("--model", default="large-v3-turbo", choices=MODELS,
                    help="model to install (default: large-v3-turbo)")
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

    say("VibeForma — installation%s" % ("  (dry-run: plan only)" if args.dry_run else ""))
    say("Target folder: %s" % target)
    say("Model: %s (~%.1f GB)   Device: %s (%s)" % (args.model, MODEL_GB[args.model], device, compute))
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
        dest = os.path.join(target, "models", args.model.replace("/", "--"))
        if os.path.exists(os.path.join(dest, "model.bin")):
            say("   model already present in %s" % dest)
            return
        probe = subprocess.run([vp, "-c", PROBE_SNIPPET, args.model],
                               capture_output=True, text=True)
        if "CACHED" in probe.stdout:
            say("   model already in the shared Hugging Face cache — "
                "the service will load it offline")
            return
        say("   downloading to %s (first-time install, the only step using the network)"
            % dest)
        subprocess.run([vp, "-c", DOWNLOAD_SNIPPET, args.model, dest], check=True)

    def write_config():
        cfg_path = os.path.join(target, "config.json")
        if os.path.exists(cfg_path):
            say("   config.json already exists — not touching it")
            return
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"model": args.model, "device": device,
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
    step("Model %s" % args.model, "probe the Hugging Face cache; if missing, "
         "download to %s\\models\\%s (~%.1f GB, the only step using the network)"
         % (target, args.model, MODEL_GB[args.model]), model_step)
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
