"""Support diagnostics: a local report without dictated text or window titles.

Run it as ``run.cmd --doctor`` (source install) or ``VibeForma.exe --doctor``
(packed build; the report is also written to ``%TEMP%\\vibeforma-doctor.txt``
because a windowed exe has no console). The settings window and the tray
show the same report with a "Copy report" button.

The report is read-only: it never loads the model, never downloads, never
records, never touches the clipboard and never prints ``hotwords``, the
dictionary, the dictation history, the HTTP token or any window title.
Exit code 0 means no failed check; 1 means at least one ``FAIL`` line.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

# Import name -> package name for a readable version line.
DEPENDENCIES = (
    ("faster_whisper", "faster-whisper", FAIL),
    ("ctranslate2", "ctranslate2", FAIL),
    ("sounddevice", "sounddevice", FAIL),
    ("numpy", "numpy", FAIL),
    ("pyperclip", "pyperclip", FAIL),
    ("pystray", "pystray", FAIL),
    ("PIL", "Pillow", FAIL),
    ("huggingface_hub", "huggingface-hub", FAIL),
    ("tkinter", "tkinter", WARN),
)

FREE_SPACE_WARN_GB = 2.0


def render(header, items) -> str:
    """Report text: a header, one aligned line per check, a verdict."""
    width = max([len(str(name)) for _, name, _ in items] + [0])
    lines = list(header)
    lines.append("")
    for level, name, detail in items:
        line = f"[{level:4}] {str(name).ljust(width)}"
        if detail:
            line += " " + str(detail)
        lines.append(line.rstrip())
    failed = sum(1 for level, _, _ in items if level == FAIL)
    warned = sum(1 for level, _, _ in items if level == WARN)
    if failed:
        verdict = f"RESULT: FAIL ({failed} failed, {warned} warnings)"
    else:
        verdict = f"RESULT: OK ({warned} warnings)" if warned else "RESULT: OK"
    lines.append("")
    lines.append(verdict)
    return "\n".join(lines)


def exit_code(items) -> int:
    """0 when nothing failed; a warning alone does not fail the run."""
    return 1 if any(level == FAIL for level, _, _ in items) else 0


def _header(config_mod) -> list[str]:
    mode = "packed" if getattr(sys, "frozen", False) else "source"
    return [
        "VibeForma doctor",
        "version: " + str(config_mod.VERSION),
        "generated: " + time.strftime("%Y-%m-%d %H:%M:%S") + " (local)",
        "mode: " + mode,
        "python: " + platform.python_version() + " (" + sys.executable + ")",
        "platform: " + platform.platform(),
        "data dir: " + str(config_mod.DATA_DIR),
        "privacy: no dictated text and no window titles are read or written",
    ]


def _settings_items(cfg, config_mod):
    items = []
    path = str(config_mod.CONFIG_PATH)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                json.load(f)
            items.append((OK, "config", "readable: " + path))
        except (OSError, ValueError) as e:
            items.append((FAIL, "config",
                          "unreadable JSON (" + type(e).__name__ + "): " + path))
    else:
        items.append((OK, "config",
                      "not created yet, built-in defaults: " + path))
    items.append((OK, "settings",
                  "model=" + str(cfg.get("model"))
                  + " device=" + str(cfg.get("device"))
                  + " compute_type=" + str(cfg.get("compute_type"))
                  + " language=" + str(cfg.get("language"))))
    items.append((OK, "hotkey", str(cfg.get("hotkey"))))
    if cfg.get("http_api"):
        if str(cfg.get("http_token") or "").strip():
            items.append((OK, "http api", "on, token set (value hidden)"))
        else:
            items.append((FAIL, "http api",
                          "on without http_token - the server refuses to start"))
    else:
        items.append((OK, "http api", "off"))
    items.append((OK, "history",
                  "on (opt-in, local)" if cfg.get("history") else "off"))
    if cfg.get("log_window_titles"):
        items.append((WARN, "window titles",
                      "written to the log (log_window_titles is on)"))
    else:
        items.append((OK, "window titles", "not written to the log"))
    if cfg.get("notify_text"):
        items.append((WARN, "notifications",
                      "show the dictated text (notify_text is on)"))
    else:
        items.append((OK, "notifications", "character count only"))
    return items


def _dependency_items():
    items = []
    for module_name, package, missing_level in DEPENDENCIES:
        try:
            module = importlib.import_module(module_name)
        except Exception as e:  # noqa: BLE001 - any import failure is the finding
            items.append((missing_level, "dep " + package,
                          "missing (" + type(e).__name__ + ")"))
            continue
        version = getattr(module, "__version__", "")
        items.append((OK, "dep " + package, str(version) if version else "present"))
    return items


def _model_items(cfg, config_mod):
    items = []
    name = str(cfg.get("model") or "")
    local = os.path.join(str(config_mod.MODELS_DIR), name, "model.bin")
    if name and os.path.isfile(local):
        items.append((OK, "model", name + " (in the data folder)"))
        return items
    try:
        import asr
        from huggingface_hub import snapshot_download
        snapshot_download(asr._repo_id(name), local_files_only=True)
        items.append((OK, "model", name + " (shared Hugging Face cache)"))
    except Exception:  # noqa: BLE001 - absent model is a warning, not a crash
        items.append((WARN, "model",
                      name + " is not on disk yet - it downloads on first start"))
    return items


def _hardware_items(cfg):
    items = []
    cuda = 0
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - a missing engine is reported by deps
        cuda = 0
    device = str(cfg.get("device") or "")
    compute = str(cfg.get("compute_type") or "")
    if device == "cuda":
        if cuda >= 1:
            items.append((OK, "device",
                          "cuda (" + str(cuda) + " device(s)), compute_type="
                          + compute))
        else:
            items.append((FAIL, "device",
                          "configured cuda but no CUDA device was found - "
                          "reinstall with --cpu or set device=cpu, "
                          "compute_type=int8"))
    else:
        level = OK if compute == "int8" else WARN
        detail = "cpu, compute_type=" + compute
        if level == WARN:
            detail += " (int8 is the usual CPU profile)"
        items.append((level, "device", detail))
    return items


def _microphone_items(cfg):
    items = []
    try:
        import sounddevice as sd
        devices = list(sd.query_devices())
        inputs = [d for d in devices if int(d.get("max_input_channels", 0)) > 0]
    except Exception as e:  # noqa: BLE001 - PortAudio has no stable base exception
        return [(FAIL, "microphone",
                 "could not query input devices (" + type(e).__name__ + ")")]
    if not inputs:
        return [(FAIL, "microphone", "no input devices found")]
    try:
        default_in = sd.default.device[0]
        default_name = str(sd.query_devices(default_in)["name"])
    except Exception:  # noqa: BLE001 - no default is a finding, not a crash
        default_name = "none"
    selected = str(cfg.get("input_device") or "").strip()
    if selected:
        found = any(selected.lower() in str(d.get("name", "")).lower()
                    for d in inputs)
        if not found:
            items.append((WARN, "microphone",
                          str(len(inputs)) + " input device(s), default '"
                          + default_name + "'; input_device '" + selected
                          + "' not found - the default is used"))
        else:
            items.append((OK, "microphone",
                          str(len(inputs)) + " input device(s), default '"
                          + default_name + "', selected '" + selected + "'"))
    else:
        items.append((OK, "microphone",
                      str(len(inputs)) + " input device(s), default '"
                      + default_name + "'"))
    return items


def _hotkey_items(cfg):
    combo = str(cfg.get("hotkey") or "")
    try:
        import onboarding
        valid, code, warning = onboarding.validate_hotkey(combo)
    except Exception as e:  # noqa: BLE001 - the check itself reports failures
        return [(WARN, "hotkey", "could not validate (" + type(e).__name__ + ")")]
    if not valid:
        return [(FAIL, "hotkey", "'" + combo + "' is invalid (" + code + ")")]
    if warning:
        return [(WARN, "hotkey",
                 "'" + combo + "' may be reserved by Windows")]
    return [(OK, "hotkey", "'" + combo + "' parses")]


def _update_items(cfg):
    if not cfg.get("update_check"):
        return [(OK, "update", "automatic checks off")]
    mode = str(cfg.get("auto_update") or "notify")
    return [(OK, "update",
             "check every " + str(cfg.get("update_interval_h")) + " h, mode "
             + mode)]


def _storage_items(config_mod):
    # Read-only: the report never creates the data or log folder.
    items = []
    data_dir = str(config_mod.DATA_DIR)
    if not os.path.isdir(data_dir):
        items.append((OK, "data dir",
                      "not created yet (appears on the first save): "
                      + data_dir))
    else:
        try:
            handle, probe = tempfile.mkstemp(prefix=".doctor-", dir=data_dir)
            os.close(handle)
            os.remove(probe)
            items.append((OK, "data dir", "writable: " + data_dir))
        except OSError as e:
            items.append((FAIL, "data dir",
                          "not writable (" + type(e).__name__ + "): "
                          + data_dir))
    log_dir = str(config_mod.LOG_DIR)
    if os.path.isdir(log_dir):
        items.append((OK, "log dir", str(log_dir)))
    else:
        items.append((OK, "log dir",
                      "not created yet (appears on the first start): "
                      + log_dir))
    try:
        free_gb = shutil.disk_usage(data_dir).free / float(1 << 30)
        level = OK if free_gb >= FREE_SPACE_WARN_GB else WARN
        items.append((level, "free space", f"{free_gb:.1f} GB"))
    except OSError:
        items.append((WARN, "free space", "unknown"))
    if os.path.isfile(str(config_mod.TEST_WAV)):
        items.append((OK, "selftest WAV", "present"))
    else:
        items.append((WARN, "selftest WAV",
                      "missing - --selftest cannot run"))
    return items


def collect(cfg=None):
    """(header lines, check items) for the machine as it is right now."""
    import config as config_mod
    cfg = dict(cfg) if cfg is not None else config_mod.load()
    items = []
    items.extend(_settings_items(cfg, config_mod))
    items.extend(_dependency_items())
    items.extend(_model_items(cfg, config_mod))
    items.extend(_hardware_items(cfg))
    items.extend(_microphone_items(cfg))
    items.extend(_hotkey_items(cfg))
    items.extend(_update_items(cfg))
    items.extend(_storage_items(config_mod))
    return _header(config_mod), items


def main(report_path=None) -> int:
    """Print the report; optionally write it to a file (the packed build)."""
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    header, items = collect()
    text = render(header, items)
    print(text)
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print("Report written to: " + str(report_path))
        except OSError as e:
            print("Could not write the report: " + type(e).__name__)
            return 1
    return exit_code(items)


def show_report(master, tr, log):
    """Tk window with the report and a Copy button; returns the Toplevel."""
    import tkinter as tk

    bg, fg, field = "#16181D", "#D5D9E0", "#22262E"
    header, items = collect()
    text = render(header, items)
    win = tk.Toplevel(master)
    win.title(tr("doctor.title"))
    win.configure(bg=bg)
    body = tk.Frame(win, bg=bg)
    body.pack(fill="both", expand=True, padx=12, pady=(10, 4))
    box = tk.Text(body, width=90, height=30, bg=field, fg=fg, relief="flat",
                  wrap="none", font=("Consolas", 10), insertbackground=fg)
    scroll = tk.Scrollbar(body, command=box.yview)
    box.configure(yscrollcommand=scroll.set)
    box.insert("1.0", text)
    box.configure(state="disabled")
    box.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    bar = tk.Frame(win, bg=bg)
    bar.pack(fill="x", padx=12, pady=(0, 10))
    status = tk.Label(bar, text=tr("doctor.note"), font=("Segoe UI", 9),
                      fg="#9FE0A8", bg=bg, anchor="w")

    def copy_report():
        win.clipboard_clear()
        win.clipboard_append(text)
        status.configure(text=tr("doctor.copied"))

    tk.Button(bar, text=tr("doctor.copy"), command=copy_report, width=18,
              bg="#2E86DE", fg="white", relief="flat",
              activebackground="#1B6EC2", activeforeground="white",
              cursor="hand2").pack(side="right", padx=(8, 0))
    tk.Button(bar, text=tr("doctor.close"), command=win.destroy, width=10,
              bg=field, fg=fg, relief="flat", activebackground="#2A2F38",
              activeforeground=fg, cursor="hand2").pack(side="right")
    status.pack(side="left")
    log.info("Doctor report opened: %s",
             "FAIL" if exit_code(items) else "OK")
    return win
