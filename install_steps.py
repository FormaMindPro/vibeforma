"""Installer steps: copy, venv, dependency and model steps, plan."""
import contextlib
import datetime
import json
import os
import shutil
import sys
import tempfile

from install_core import (
    APP_DIR,
    DOWNLOAD_SNIPPET,
    INSTALL_MARKER,
    LNKS,
    MODEL_GB,
    OWN_DIRS,
    PROBE_SNIPPET,
    PRODUCT_DIRS,
    PRODUCT_FILES,
    REPO_DIR,
    InstallError,
    app_version,
    run_logged,
    step,
)
from install_system import legacy_detected


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
