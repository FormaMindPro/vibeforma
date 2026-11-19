"""Release update checker: GitHub latest release, apply while idle.

The background loop in main.py calls latest_release() every
update_interval_h hours (default 2). When a newer release is found, apply()
downloads the release zip and runs install.py against the current service
folder; the service then restarts itself. Failures are logged, never fatal.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

REPO_API = "https://api.github.com/repos/FormaMindPro/vibeforma/releases/latest"
_UA = {"User-Agent": "VibeForma-updater"}


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
    """Latest GitHub release: {'tag', 'version', 'zip', 'html'} or None."""
    req = urllib.request.Request(REPO_API, headers=_UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    ver = version_tuple(data.get("tag_name", ""))
    if not ver:
        return None
    asset = next((a for a in data.get("assets", [])
                  if str(a.get("name", "")).startswith("VibeForma-")
                  and str(a.get("name", "")).endswith(".zip")), None)
    return {
        "tag": data.get("tag_name", ""),
        "version": ver,
        "zip": asset.get("browser_download_url") if asset else None,
        "html": data.get("html_url", ""),
    }


def apply(release, service_dir, log: logging.Logger):
    """Download the release zip and run install.py against service_dir.

    install.py skips what already exists (venv, config, cached model), so
    an update is cheap: copy app files, refresh shortcuts.
    """
    import zipfile

    tmp = tempfile.mkdtemp(prefix="VibeForma-update-")
    try:
        if not release.get("zip"):
            log.warning("Update: release %s has no zip asset", release.get("tag"))
            return False
        zp = os.path.join(tmp, "update.zip")
        req = urllib.request.Request(release["zip"], headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as r, open(zp, "wb") as f:
            shutil.copyfileobj(r, f)
        xdir = os.path.join(tmp, "x")
        with zipfile.ZipFile(zp) as z:
            z.extractall(xdir)
        src = None
        for n in os.listdir(xdir):
            if os.path.exists(os.path.join(xdir, n, "install.py")):
                src = os.path.join(xdir, n)
                break
        if not src:
            log.warning("Update: install.py not found in the release zip")
            return False
        log.info("Update: applying %s to %s", release["tag"], service_dir)
        r = subprocess.run(
            [sys.executable, "install.py", "--target", service_dir, "--no-run"],
            cwd=src, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            log.warning("Update: installer exit %s: %s",
                        r.returncode, (r.stdout or r.stderr)[-500:])
            return False
        log.info("Update applied to %s", service_dir)
        return True
    except Exception as e:
        log.warning("Update apply failed: %s", e)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
