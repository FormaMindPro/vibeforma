"""Release update checker: GitHub latest release, verified apply while idle.

The background loop in main.py checks every update_interval_h hours
(default 2). A check only records a pending release; applying it requires
the user's consent (auto_update == "notify", the default) or an explicit
opt-in (auto_update == "auto"). Before any installer code runs, the zip is
authenticated against a signed manifest and a compiled-in public key; on
failure the previous installation is restored from a backup.

The network is used only for the release metadata and assets, as before.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import release_verify

REPO_API = "https://api.github.com/repos/FormaMindPro/vibeforma/releases/latest"
_UA = {"User-Agent": "VibeForma-updater"}
PRODUCT_FILES = ("run.cmd", "requirements.txt", "test_ru.wav", "config.json")
PRODUCT_DIRS = ("assets", "locales", "app")
SKIP_DIRS = {"__pycache__", ".venv", "models", "logs"}
SKIP_FILES = {"service.lock", "config.json.tmp"}


def version_tuple(v):
    """'v0.1.3' or '0.1.3' -> (0, 1, 3); None on garbage."""
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return None


def is_newer(latest, current):
    """Tuple comparison: does 'latest' beat 'current'."""
    return latest > current


def latest_release():
    """Latest GitHub release: tag, version, asset URLs; None if unusable."""
    req = urllib.request.Request(REPO_API, headers=_UA)
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
        data = json.load(r)
    ver = version_tuple(data.get("tag_name", ""))
    if not ver:
        return None
    assets = {str(a.get("name", "")): a.get("browser_download_url")
              for a in data.get("assets", [])}
    zip_name = next((name for name in assets
                     if name.startswith("VibeForma-")
                     and name.endswith(".zip")), None)
    return {
        "tag": data.get("tag_name", ""),
        "version": ver,
        "zip": assets.get(zip_name) if zip_name else None,
        "manifest": assets.get("release-manifest.json"),
        "signature": assets.get("release-manifest.json.minisig"),
        "html": data.get("html_url", ""),
    }


def download_asset(url, path, timeout):
    req = urllib.request.Request(  # noqa: S310 - https release assets
        url, headers=_UA)
    response = urllib.request.urlopen(  # noqa: S310 - https release assets
        req, timeout=timeout)
    with response as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)


def verify_release_files(zip_path, manifest_path, signature_path, tag,
                         public_key_b64=None):
    """Authenticate the manifest, hash and layout; returns the manifest."""
    with open(manifest_path, "rb") as f:
        manifest_bytes = f.read()
    with open(signature_path, encoding="utf-8") as f:
        signature_text = f.read()
    release_verify.verify_manifest(
        manifest_bytes, signature_text, public_key_b64)
    manifest = release_verify.parse_manifest(manifest_bytes)
    release_verify.version_matches(manifest, tag)
    expected_zip = f"VibeForma-{manifest['version']}.zip"
    if manifest["zip"] not in (os.path.basename(zip_path), expected_zip):
        raise release_verify.ReleaseError(
            "release manifest names a different zip")
    release_verify.verify_zip(zip_path, manifest)
    return manifest


def _product_entries(service_dir):
    entries = []
    for name in sorted(os.listdir(service_dir)):
        path = os.path.join(service_dir, name)
        if name in SKIP_FILES or name in SKIP_DIRS:
            continue
        if os.path.isdir(path):
            if name in PRODUCT_DIRS:
                entries.append(name)
        elif name.endswith(".py") or name in PRODUCT_FILES:
            entries.append(name)
    return entries


def backup_service(service_dir, backup_dir):
    """Copy product files and config aside; returns the saved entry names."""
    os.makedirs(backup_dir, exist_ok=True)
    names = _product_entries(service_dir)
    for name in names:
        src = os.path.join(service_dir, name)
        dst = os.path.join(backup_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return names


def restore_service(service_dir, backup_dir, names):
    """Put the saved entries back and drop product files added by a failed run."""
    for name in _product_entries(service_dir):
        if name not in names:
            path = os.path.join(service_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    for name in names:
        src = os.path.join(backup_dir, name)
        dst = os.path.join(service_dir, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _run_installer(src, service_dir):
    r = subprocess.run(  # noqa: S603 - a fixed interpreter and argument list
        [sys.executable, "install.py", "--target", service_dir, "--no-run"],
        cwd=src, capture_output=True, text=True, timeout=1800)
    return r.returncode, (r.stdout or r.stderr or "")[-500:]


def apply(release, service_dir, log: logging.Logger, installer=None,
          public_key_b64=None):
    """Verified download, backup, install; rollback on failure.

    Returns (ok, reason). The reason is English and meant for the log; the
    UI shows a localized message and never raw network details.
    """
    installer = installer or _run_installer
    if not release.get("zip"):
        return False, "release has no zip asset"
    if not release.get("manifest") or not release.get("signature"):
        return False, "release has no manifest or signature asset"
    if public_key_b64 is None and not release_verify.RELEASE_PUBLIC_KEY:
        return False, "release signing key is not configured; update refused"

    tmp = tempfile.mkdtemp(prefix="VibeForma-update-")
    try:
        zp = os.path.join(tmp, "update.zip")
        mp = os.path.join(tmp, "release-manifest.json")
        sp = os.path.join(tmp, "release-manifest.json.minisig")
        download_asset(release["zip"], zp, 60)
        download_asset(release["manifest"], mp, 30)
        download_asset(release["signature"], sp, 30)
        manifest = verify_release_files(
            zp, mp, sp, release["tag"], public_key_b64)
        src = release_verify.extract_verified(
            zp, manifest, os.path.join(tmp, "x"))

        backup_dir = os.path.join(tmp, "backup")
        saved = backup_service(service_dir, backup_dir)
        log.info("Update: applying %s to %s", release["tag"], service_dir)
        code, output = installer(src, service_dir)
        if code != 0:
            restore_service(service_dir, backup_dir, saved)
            log.warning("Update: installer exit %s, previous files restored: %s",
                        code, output)
            return False, f"installer exit {code}"
        log.info("Update applied to %s", service_dir)
        return True, ""
    except release_verify.ReleaseError as e:
        log.warning("Update refused: %s", e)
        return False, str(e)
    except Exception as e:
        log.warning("Update apply failed: %s", e, exc_info=True)
        return False, str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
