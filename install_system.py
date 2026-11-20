"""Installer system: legacy migration, uninstall entry, shortcuts."""
import contextlib
import json
import os
import shutil

from install_core import (
    LEGACY_DIR_NAME,
    LEGACY_LINKS_PS,
    LNKS,
    SHORTCUTS_PS,
    UNINSTALL_KEY,
    UNINSTALL_VALUE_NAME,
    app_version,
    run_logged,
    write_ps1,
)


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
        import winreg as winreg_mod
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
        import winreg as winreg_mod
    try:
        key = winreg_mod.OpenKey(winreg_mod.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                                 winreg_mod.KEY_ALL_ACCESS)
    except OSError:
        return False
    winreg_mod.CloseKey(key)
    winreg_mod.DeleteKey(winreg_mod.HKEY_CURRENT_USER, UNINSTALL_KEY)
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
    os.startfile(  # noqa: S606 - the installer launches its own run.cmd
        os.path.join(target, "run.cmd"))
