"""Release update checker: GitHub latest release and the README install.

The background loop in main.py checks every update_interval_h hours
(default 2). A check only records a pending release; applying it requires
the user's consent (auto_update == "notify", the default) or an explicit
opt-in (auto_update == "auto"). Applying runs the same plain GitHub
installation command as the README (install.ps1).

The network carries the release metadata, the zip and its SHA256SUMS. The
zip is verified locally before the bootstrap sees it; the verified paths
travel through the environment, never through the command line.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.request

REPO_API = "https://api.github.com/repos/FormaMindPro/vibeforma/releases/latest"
BOOTSTRAP_URL = ("https://raw.githubusercontent.com/FormaMindPro/"
                 "vibeforma/main/install.ps1")
UPDATE_TARGET_ENV = "VIBEFORMA_UPDATE_TARGET"
UPDATE_ZIP_ENV = "VIBEFORMA_UPDATE_ZIP"
UPDATE_SUMS_ENV = "VIBEFORMA_UPDATE_SUMS"
SUMS_NAME = "SHA256SUMS"
INSTALL_MARKER = ".vibeforma-install.json"
_UA = {"User-Agent": "VibeForma-updater"}
_SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")


def version_tuple(v):
    """'v0.1.3' or '0.1.3' -> (0, 1, 3); None on garbage."""
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return None


def is_newer(latest, current):
    """Tuple comparison: does 'latest' beat 'current'."""
    return latest > current


def github_install_command(target_env=UPDATE_TARGET_ENV,
                           zip_env=UPDATE_ZIP_ENV,
                           sums_env=UPDATE_SUMS_ENV):
    """PowerShell command running the README bootstrap for an existing install.

    The target folder and the verified local zip/sums travel through the
    environment, so a path with spaces or quotes never becomes part of the
    command line.
    """
    script = ("$s = irm '" + BOOTSTRAP_URL + "'; "
              "& ([scriptblock]::Create($s)) "
              "-Target $env:" + target_env
              + " -ZipPath $env:" + zip_env
              + " -SumsPath $env:" + sums_env
              + " -NoRun")
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
        "sums": assets.get(SUMS_NAME),
        "html": data.get("html_url", ""),
        "body": data.get("body", ""),
    }


def parse_sha256sums(text):
    """``hash  name`` lines into {name: lower-case hash}; broken lines skipped."""
    result = {}
    for raw in str(text or "").splitlines():
        match = _SUMS_LINE.match(raw.strip())
        if match:
            result[match.group(2)] = match.group(1).lower()
    return result


def expected_sha256(text, asset_name):
    """The sums hash for one asset name, lower-case; None when absent."""
    name = str(asset_name or "").strip()
    if not name:
        return None
    sums = parse_sha256sums(text)
    digest = sums.get(name) or sums.get(os.path.basename(name))
    return digest.lower() if digest else None


def sha256_file(path):
    """Streaming SHA-256 of a file in 1 MiB blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sha256(path, expected):
    """Whether the file's hash equals ``expected``; False on an empty value."""
    if not expected:
        return False
    return sha256_file(path) == str(expected).strip().lower()


def download_file(url, dest, timeout=300):
    """Download an https URL to ``dest`` atomically; a part file never stays."""
    if urllib.parse.urlsplit(str(url)).scheme != "https":
        raise ValueError(f"refusing a non-https download: {url}")
    part = str(dest) + ".part"
    request = urllib.request.Request(str(url), headers=_UA)  # noqa: S310
    try:
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,  # noqa: S310
            open(part, "wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
        os.replace(part, dest)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(part)
        raise


def is_source_checkout(service_dir):
    """A git checkout (pyproject beside app/) that was never installed.

    The install marker is what distinguishes an installed copy: a source
    checkout must not be overwritten by a release zip.
    """
    service_dir = os.path.abspath(str(service_dir))
    parent = os.path.dirname(service_dir)
    return (os.path.isfile(os.path.join(parent, "pyproject.toml"))
            and not os.path.isfile(os.path.join(service_dir, INSTALL_MARKER)))


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
