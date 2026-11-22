"""Release update checker: GitHub latest release and the README install.

The background loop in main.py checks every update_interval_h hours
(default 2). A check only records a pending release; applying it requires
the user's consent (auto_update == "notify", the default) or an explicit
opt-in (auto_update == "auto"). Applying runs the same plain GitHub
installation command as the README (install.ps1).

The network is used only for the release metadata, as before.
"""
import json
import re
import urllib.request

REPO_API = "https://api.github.com/repos/FormaMindPro/vibeforma/releases/latest"
BOOTSTRAP_URL = ("https://raw.githubusercontent.com/FormaMindPro/"
                 "vibeforma/main/install.ps1")
UPDATE_TARGET_ENV = "VIBEFORMA_UPDATE_TARGET"
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


def github_install_command(target_env=UPDATE_TARGET_ENV):
    """PowerShell command running the README bootstrap for an existing install.

    The target folder travels through the environment, so a path with
    spaces or quotes never becomes part of the command line.
    """
    script = ("$s = irm '" + BOOTSTRAP_URL + "'; "
              "& ([scriptblock]::Create($s)) "
              "-Target $env:" + target_env + " -NoRun")
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", script]


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
        "html": data.get("html_url", ""),
        "body": data.get("body", ""),
    }


def release_notes_url(release):
    """The https release page URL, or '' when the release carries none."""
    url = str((release or {}).get("html") or "").strip()
    return url if url.startswith("https://") else ""


def changelog_excerpt(body, limit=240):
    """Plain one-line excerpt of the release notes (the CHANGELOG body)."""
    lines = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        while line.startswith("#"):
            line = line[1:].strip()
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = line.replace("**", "").replace("`", "")
        line = line.lstrip("-*+ ").strip()
        if line and not line.lower().startswith("full changelog"):
            lines.append(line)
    text = " / ".join(lines)
    if limit and len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text
