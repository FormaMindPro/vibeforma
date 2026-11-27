#!/usr/bin/env python
"""Build the release zip and its SHA256SUMS.

The release is a plain GitHub zip plus the checksum file next to it.
"""
import argparse
import hashlib
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
TOP_FILES = (
    "install.py",
    "install_core.py",
    "install_env.py",
    "install_steps.py",
    "install_system.py",
    "install.cmd",
    "README.md",
    "README.ru.md",
    "CHANGELOG.md",
    "LICENSE",
    "test_ru.wav",
)
SKIP_DIRS = {"__pycache__", "logs", "models", "assets-cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".tmp")

# Whitelist of the app/ payload. A development install keeps personal
# data next to the code (config.json, stats.json, history.jsonl,
# history_audio/, corrections.json, service.lock), so anything found in
# app/ outside this list fails the build instead of being skipped or,
# worse, shipped.
#
# The personal files below are the known exception: they are never
# shipped, never fail the build, and are reported by name when the
# builder sees them in a development tree.
APP_FILES = (
    "app/asr.py",
    "app/audio.py",
    "app/cancel_hotkey.py",
    "app/config.py",
    "app/dictlearn.py",
    "app/dictionary.py",
    "app/doctor.py",
    "app/errors.py",
    "app/file_jobs.py",
    "app/hallucination.py",
    "app/history.py",
    "app/hook.py",
    "app/hotkey.py",
    "app/hud.py",
    "app/i18n.py",
    "app/insert.py",
    "app/keymap.py",
    "app/main.py",
    "app/mcp.cmd",
    "app/model_catalog.py",
    "app/model_folders.py",
    "app/onboarding.py",
    "app/requirements-cuda.in",
    "app/requirements-cuda.txt",
    "app/requirements.in",
    "app/requirements.txt",
    "app/run.cmd",
    "app/settings_window.py",
    "app/snippets.py",
    "app/snippets_ui.py",
    "app/stats.py",
    "app/stats_ui.py",
    "app/system_audio.py",
    "app/system_recording.py",
    "app/textclean.py",
    "app/textrules.py",
    "app/tray.py",
    "app/transcript.py",
    "app/uninstall.cmd",
    "app/uninstall.py",
    "app/update_control.py",
    "app/updater.py",
    "app/window_layout.py",
    "app/winfx.py",
    "app/assets/icon.ico",
    "app/assets/icon.png",
    "app/locales/en.json",
    "app/locales/ru.json",
    "app/vibeforma/__init__.py",
    "app/vibeforma/actions.py",
    "app/vibeforma/app.py",
    "app/vibeforma/base.py",
    "app/vibeforma/connect.py",
    "app/vibeforma/http_api.py",
    "app/vibeforma/mcp_server.py",
    "app/vibeforma/model_updates.py",
    "app/vibeforma/pipe.py",
    "app/vibeforma/selftest.py",
    "app/vibeforma/service.py",
    "app/vibeforma/settings_ops.py",
    "app/vibeforma/status.py",
    "app/vibeforma/update_ops.py",
    "app/vibeforma/updates.py",
    "app/vibeforma/util.py",
)

# Personal runtime names relative to app/ (the whitelist uses "app/..." form).
APP_PERSONAL_FILES = (
    "config.json",
    "stats.json",
    "history.jsonl",
    "corrections.json",
    "service.lock",
)
APP_PERSONAL_DIRS = ("history_audio/",)


def is_personal_app_file(rel):
    """True for known personal runtime files left in app/ by a dev install."""
    if not rel.startswith("app/"):
        return False
    tail = rel[len("app/"):]
    return tail in APP_PERSONAL_FILES or tail.startswith(APP_PERSONAL_DIRS)


def read_version():
    with open(os.path.join(APP, "config.py"), encoding="utf-8") as f:
        text = f.read()
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("build: VERSION not found in app/config.py")
    return match.group(1)


def app_files():
    allowed = set(rel for rel in APP_FILES
                  if os.path.isfile(os.path.join(ROOT, rel)))
    found = set()
    personal = set()
    for base, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(SKIP_SUFFIX):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if is_personal_app_file(rel):
                personal.add(rel)
            else:
                found.add(rel)
    unexpected = sorted(found - allowed)
    if unexpected:
        raise SystemExit("build: files outside the app/ whitelist: "
                         + ", ".join(unexpected))
    if personal:
        print("build: excluded personal files: " + ", ".join(sorted(personal)))
    return sorted(allowed)


def collect(version):
    missing = []
    files = []
    for name in TOP_FILES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            missing.append(name)
        else:
            files.append(name)
    files.extend(app_files())
    if missing:
        raise SystemExit(f"build: missing release files: {', '.join(missing)}")
    with open(os.path.join(APP, "main.py"), encoding="utf-8") as f:
        if '"--selftest"' not in f.read():
            raise SystemExit("build: app/main.py lost the --selftest gate")
    if not os.path.getsize(os.path.join(ROOT, "test_ru.wav")):
        raise SystemExit("build: test_ru.wav is empty")
    return files


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(version, files, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    prefix = f"VibeForma-{version}"
    zip_name = prefix + ".zip"
    zip_path = os.path.join(out_dir, zip_name)
    arc_prefix = prefix + "/"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in files:
            archive.write(os.path.join(ROOT, rel), arc_prefix + rel)
    digest = sha256_file(zip_path)
    sums_path = os.path.join(out_dir, "SHA256SUMS")
    with open(sums_path, "w", encoding="ascii", newline="\n") as f:
        f.write(f"{digest}  {zip_name}\n")
    return zip_path, sums_path, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"))
    parser.add_argument("--check", action="store_true",
                        help="validate inputs without writing an archive")
    args = parser.parse_args()

    version = read_version()
    files = collect(version)
    if args.check:
        print(f"build: {len(files)} files, version {version}, inputs OK")
        return 0
    zip_path, sums_path, digest = build(
        version, files, os.path.abspath(args.out))
    print(f"build: {zip_path}")
    print(f"build: {sums_path}")
    print(f"build: sha256 {digest}, {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
