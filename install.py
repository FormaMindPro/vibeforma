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
restored from a backup. Every step and error tail goes to install.log in
%LOCALAPPDATA%\\VibeForma\\logs (override with --log).
"""
import argparse
import json
import os
import shutil
import subprocess  # noqa: F401 - tests patch install.subprocess
import sys

from install_core import (
    APP_DIR,
    DEFAULT_MODEL,
    DEFAULT_MODEL_CPU,
    DEFAULT_TARGET,
    DOWNLOAD_SNIPPET,
    FREE_SPACE_HEADROOM_GB,
    INSTALL_LOG_NAME,
    INSTALL_MARKER,
    LEGACY_DIR_NAME,
    LEGACY_LINKS_PS,
    LNKS,
    LOG_TAIL_LINES,
    MODEL_GB,
    MODELS,
    OWN_DIRS,
    PROBE_SNIPPET,
    PRODUCT_DIRS,
    PRODUCT_FILES,
    PYTHON_MAX_EXCLUSIVE,
    PYTHON_MIN,
    REPO_DIR,
    SHORTCUTS_PS,
    SUPPORTED_ARCH,
    UNINSTALL_KEY,
    UNINSTALL_VALUE_NAME,
    WINDOWS_MIN_BUILD,
    CommandFailed,
    InstallError,
    InstallLog,
    StepFailed,
    _quote,
    _tail,
    app_version,
    log_path_default,
    model_description,
    run_logged,
    say,
    step,
    write_ps1,
)
from install_env import (
    INSTALLER_TEXTS,
    _canon,
    _existing_ancestor,
    ask_console,
    check_arch,
    check_disk,
    check_python_version,
    check_windows,
    check_writable,
    choose_setup,
    default_model,
    detect_compute,
    free_bytes,
    installer_language,
    installer_text,
    language_from_locale,
    looks_like_install,
    protected_reason,
    stdin_tty,
    supported_languages,
    target_state,
    windows_language,
)
from install_steps import (
    backup_target,
    cleanup_fresh,
    copy_app,
    copy_test_wav,
    make_venv,
    model_step,
    pip_install,
    plan,
    product_entries,
    requirements_file,
    restore_target,
    run_selftest,
    write_config,
    write_marker,
)
from install_system import (
    legacy_detected,
    legacy_dir,
    make_shortcuts,
    migrate_legacy,
    remove_legacy_shortcuts,
    remove_uninstall_entry,
    run_service,
    uninstall_entry_values,
    write_uninstall_entry,
)

__all__ = [
    APP_DIR, CommandFailed, DEFAULT_MODEL, DEFAULT_MODEL_CPU,
    DEFAULT_TARGET, DOWNLOAD_SNIPPET, FREE_SPACE_HEADROOM_GB, INSTALLER_TEXTS,
    INSTALL_LOG_NAME, INSTALL_MARKER, InstallError, InstallLog,
    LEGACY_DIR_NAME, LEGACY_LINKS_PS, LNKS, LOG_TAIL_LINES,
    MODELS, MODEL_GB, OWN_DIRS, PROBE_SNIPPET,
    PRODUCT_DIRS, PRODUCT_FILES, PYTHON_MAX_EXCLUSIVE, PYTHON_MIN,
    REPO_DIR, SHORTCUTS_PS, SUPPORTED_ARCH, StepFailed,
    UNINSTALL_KEY, UNINSTALL_VALUE_NAME, WINDOWS_MIN_BUILD, _canon,
    _existing_ancestor, _quote, _tail, app_version,
    ask_console, backup_target, check_arch, check_disk,
    check_python_version, check_windows, check_writable, choose_setup,
    cleanup_fresh, copy_app, copy_test_wav, default_model,
    detect_compute, free_bytes, installer_language, installer_text,
    language_from_locale, legacy_detected, legacy_dir, log_path_default,
    looks_like_install, make_shortcuts, make_venv,
    migrate_legacy, model_description, model_step, pip_install,
    plan, product_entries, protected_reason, remove_legacy_shortcuts,
    remove_uninstall_entry, requirements_file, restore_target,
    run_logged, run_selftest, run_service, say,
    stdin_tty, step, supported_languages, target_state,
    uninstall_entry_values, windows_language, write_config, write_marker,
    write_ps1, write_uninstall_entry,
]


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
