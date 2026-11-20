#!/usr/bin/env python
"""Build the release zip, SHA256SUMS and the signed manifest payload.

The manifest lists every file in the archive; the updater refuses anything
not listed and checks the zip hash before unpacking. Signing is a separate
step (release workflow, minisign) so the private key never enters the repo.
"""
import argparse
import hashlib
import json
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


def read_version():
    with open(os.path.join(APP, "config.py"), encoding="utf-8") as f:
        text = f.read()
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("build: VERSION not found in app/config.py")
    return match.group(1)


def app_files():
    result = []
    for base, dirs, files in os.walk(APP):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(base, name)
            if name.endswith(SKIP_SUFFIX) or name == "config.json":
                continue
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            result.append(rel)
    return result


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


def installer_entry(path, version):
    """The setup exe named by the signed manifest, when one was built."""
    if not path:
        return None
    name = f"VibeForma-Setup-{version}.exe"
    if os.path.basename(path) != name:
        raise SystemExit(f"build: installer name must be {name}")
    if not os.path.isfile(path):
        raise SystemExit(f"build: no installer at {path}")
    return {"name": name, "sha256": sha256_file(path),
            "size": os.path.getsize(path)}


def build(version, files, out_dir, installer=None):
    os.makedirs(out_dir, exist_ok=True)
    prefix = f"VibeForma-{version}"
    zip_name = prefix + ".zip"
    zip_path = os.path.join(out_dir, zip_name)
    arc_prefix = prefix + "/"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in files:
            archive.write(os.path.join(ROOT, rel), arc_prefix + rel)
    digest = sha256_file(zip_path)
    names = [arc_prefix + rel for rel in files]
    manifest = {
        "version": version,
        "zip": zip_name,
        "sha256": digest,
        "size": os.path.getsize(zip_path),
        "files": sorted(names),
    }
    if installer:
        manifest["installer"] = installer
    manifest_path = os.path.join(out_dir, "release-manifest.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=True, indent=2)
        f.write("\n")
    sums_path = os.path.join(out_dir, "SHA256SUMS")
    with open(sums_path, "w", encoding="ascii", newline="\n") as f:
        f.write(f"{digest}  {zip_name}\n")
    return zip_path, manifest_path, sums_path, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"))
    parser.add_argument("--check", action="store_true",
                        help="validate inputs without writing an archive")
    parser.add_argument("--installer", default=None,
                        help="the built setup exe to include in the manifest")
    args = parser.parse_args()

    version = read_version()
    files = collect(version)
    if args.check:
        print(f"build: {len(files)} files, version {version}, inputs OK")
        return 0
    installer = installer_entry(args.installer, version)
    zip_path, manifest_path, sums_path, digest = build(
        version, files, os.path.abspath(args.out), installer)
    print(f"build: {zip_path}")
    print(f"build: {manifest_path}")
    print(f"build: {sums_path}")
    print(f"build: sha256 {digest}, {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
