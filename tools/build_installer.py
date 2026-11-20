#!/usr/bin/env python
"""Build the packed application and the signed Windows setup.

Steps:
  1. PyInstaller one-folder build (packaging/vibeforma.spec).
  2. Inno Setup per-user installer (packaging/installer.iss).
  3. Optional Authenticode signing (signtool) - fail-closed: --sign without
     a certificate stops the build instead of shipping an unsigned file.
  4. dist/installer-manifest.json with the hash and size.
  5. --winget writes winget manifests with the real hash into dist/winget.

The private key never enters the repository: CI passes it through secrets.
"""
import argparse
import contextlib
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
DIST = os.path.join(ROOT, "dist")
SPEC = os.path.join(ROOT, "packaging", "vibeforma.spec")
ISS = os.path.join(ROOT, "packaging", "installer.iss")
APP_DIST = os.path.join(DIST, "VibeForma")
VERSION_FILE = os.path.join(DIST, "build", "version_info.txt")
BUNDLE_REPORT = os.path.join(tempfile.gettempdir(),
                             "vibeforma-bundle-check.txt")
TIMESTAMP_URL = "http://timestamp.digicert.com"
REPO_URL = "https://github.com/FormaMindPro/vibeforma"
PACKAGE_ID = "FormaMindPro.VibeForma"


def read_version():
    with open(os.path.join(APP, "config.py"), encoding="utf-8") as f:
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    if not match:
        raise SystemExit("build_installer: VERSION not found in app/config.py")
    return match.group(1)


def version_tuple(version):
    return tuple(int(part) for part in version.split("."))


def setup_name(version):
    return f"VibeForma-Setup-{version}.exe"


def app_name():
    return "VibeForma.exe"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_resource(version):
    """A PyInstaller VSVersionInfo block for the exe properties."""
    nums = version_tuple(version) + (0, 0, 0)
    quad = ", ".join(str(n) for n in nums[:4])
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({quad}), prodvers=({quad}),
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'FormaMindPro'),
    StringStruct('FileDescription', 'VibeForma'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'VibeForma'),
    StringStruct('OriginalFilename', 'VibeForma.exe'),
    StringStruct('ProductName', 'VibeForma'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""


def find_tool(env_name, default_names, patterns):
    explicit = os.environ.get(env_name)
    if explicit and os.path.isfile(explicit):
        return explicit
    for name in default_names:
        for folder in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    for pattern in patterns:
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


def find_iscc():
    return find_tool(
        "VIBEFORMA_ISCC", ("ISCC.exe",),
        (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                      "Inno Setup 6", "ISCC.exe"),
         r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
         r"C:\Program Files\Inno Setup 6\ISCC.exe"))


def find_signtool():
    return find_tool(
        "VIBEFORMA_SIGNTOOL", ("signtool.exe",),
        (r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe",
         r"C:\Program Files\Windows Kits\10\bin\*\x64\signtool.exe"))


def certificate_config():
    """Signing material from the environment; private values stay in CI."""
    return {
        "pfx": os.environ.get("VIBEFORMA_CERT_PFX", ""),
        "password": os.environ.get("VIBEFORMA_CERT_PASSWORD", ""),
        "thumbprint": os.environ.get("VIBEFORMA_CERT_THUMBPRINT", ""),
    }


def signing_configured(cert):
    return bool((cert["pfx"] and cert["password"]) or cert["thumbprint"])


def sign_args(tool, target, cert, timestamp_url=TIMESTAMP_URL):
    """The signtool command; ValueError when no certificate is configured."""
    if cert["thumbprint"]:
        selector = ["/sha1", cert["thumbprint"]]
    elif cert["pfx"] and cert["password"]:
        selector = ["/f", cert["pfx"], "/p", cert["password"]]
    else:
        raise ValueError("no signing certificate configured")
    return [tool, "sign", "/fd", "sha256", "/td", "sha256",
            "/tr", timestamp_url, *selector, target]


def installer_manifest(version, path, signed):
    return {
        "product": "VibeForma",
        "version": version,
        "installer": os.path.basename(path),
        "sha256": sha256_file(path),
        "size": os.path.getsize(path),
        "signed": bool(signed),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def winget_documents(version, sha256, size, url):
    """The three winget manifests as {file name: text}."""
    header = (f"PackageIdentifier: {PACKAGE_ID}\n"
              f"PackageVersion: {version}\n")
    version_doc = (header + "DefaultLocale: en-US\n"
                   "ManifestType: version\n"
                   "ManifestVersion: 1.6.0\n")
    installer_doc = (header +
                     "InstallerType: inno\n"
                     "Scope: user\n"
                     "InstallModes:\n"
                     "- interactive\n"
                     "- silent\n"
                     "- silentWithProgress\n"
                     "UpgradeBehavior: install\n"
                     "Installers:\n"
                     "- Architecture: x64\n"
                     f"  InstallerUrl: {url}\n"
                     f"  InstallerSha256: {sha256.upper()}\n"
                     "  InstallerSwitches:\n"
                     "    Silent: /VERYSILENT /NORESTART\n"
                     "    SilentWithProgress: /SILENT /NORESTART\n"
                     "ManifestType: installer\n"
                     "ManifestVersion: 1.6.0\n")
    def locale(code, kind="locale"):
        return (header +
                f"PackageLocale: {code}\n"
                "Publisher: FormaMindPro\n"
                "PackageName: VibeForma\n"
                "License: MIT\n"
                "ShortDescription: Local voice dictation for Windows.\n"
                "Description: Hotkey, speak, the text lands in the focused "
                "field. Audio and text stay on the machine; no account, no "
                "telemetry.\n"
                "Tags:\n"
                "- dictation\n"
                "- speech-to-text\n"
                "- whisper\n"
                f"ManifestType: {kind}\n"
                "ManifestVersion: 1.6.0\n")
    return {
        f"{PACKAGE_ID}.yaml": version_doc,
        f"{PACKAGE_ID}.installer.yaml": installer_doc,
        f"{PACKAGE_ID}.locale.en-US.yaml": locale("en-US", "defaultLocale"),
        f"{PACKAGE_ID}.locale.ru-RU.yaml": locale("ru-RU"),
    }


def validate_winget_shape(documents):
    """Cheap offline checks before winget validate (which needs network)."""
    problems = []
    for name, text in documents.items():
        for key in ("PackageIdentifier", "PackageVersion", "ManifestType",
                    "ManifestVersion"):
            if not re.search(rf"^{key}: .+$", text, re.MULTILINE):
                problems.append(f"{name}: no {key}")
        if "installer.yaml" in name:
            match = re.search(r"InstallerSha256: ([0-9A-F]+)$", text,
                              re.MULTILINE)
            if not match or not re.fullmatch(r"[0-9A-F]{64}", match.group(1)):
                problems.append(f"{name}: InstallerSha256 is not 64 hex")
            if not re.search(r"InstallerUrl: https://\S+\.exe$", text,
                             re.MULTILINE):
                problems.append(f"{name}: InstallerUrl is not an https exe")
        if ("locale." in name and "$" not in name
                and ("Publisher:" not in text
                     or "ShortDescription:" not in text)):
            problems.append(f"{name}: publisher or description missing")
    return problems


def plan(version, sign):
    return [
        f"pyinstaller {SPEC} -> {APP_DIST}",
        f"iscc packaging/installer.iss -> "
        f"{os.path.join(DIST, setup_name(version))}",
        "sign with signtool" if sign else "skip signing (no --sign)",
        "write dist/installer-manifest.json",
    ]


def run(cmd, env=None):
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, env=env, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"step failed: {cmd[0]} rc={result.returncode}")
    return result


def bundle_check(exe):
    """Run the packed exe's self-diagnosis; returns (ok, report text)."""
    with contextlib.suppress(OSError):
        os.remove(BUNDLE_REPORT)
    result = subprocess.run([exe, "--bundle-check"], timeout=300, check=False)
    text = ""
    with (contextlib.suppress(OSError),
          open(BUNDLE_REPORT, encoding="utf-8") as f):
        text = f.read().strip()
    return result.returncode == 0 and "BUNDLE OK" in text, text


def build_app(version, env):
    os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
    with open(VERSION_FILE, "w", encoding="ascii") as f:
        f.write(version_resource(version))
    env["VIBEFORMA_VERSION_FILE"] = VERSION_FILE
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         SPEC], env=env)
    exe = os.path.join(APP_DIST, app_name())
    if not os.path.isfile(exe):
        raise SystemExit(f"PyInstaller produced no {exe}")
    ok, text = bundle_check(exe)
    print("bundle check:", text or "no report")
    if not ok:
        raise SystemExit("the packed application failed its bundle check")
    return exe


def build_setup(version, iscc, env):
    # Inno's preprocessor takes defines on the command line, not from the
    # environment; pass every path explicitly (it may contain spaces).
    defines = [f"/DVIBEFORMA_VERSION={version}",
               f"/DVIBEFORMA_DIST={APP_DIST}",
               f"/DVIBEFORMA_OUT={DIST}"]
    run([iscc, *defines, ISS], env=env)
    setup = os.path.join(DIST, setup_name(version))
    if not os.path.isfile(setup):
        raise SystemExit(f"Inno Setup produced no {setup}")
    return setup


def sign_file(path, cert):
    tool = find_signtool()
    if not tool:
        raise SystemExit("signtool not found; cannot sign the installer")
    run(sign_args(tool, path, cert))
    return True


def winget_url(version):
    return f"{REPO_URL}/releases/download/v{version}/{setup_name(version)}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the steps and stop")
    parser.add_argument("--sign", action="store_true",
                        help="sign the setup (fails without a certificate)")
    parser.add_argument("--winget", action="store_true",
                        help="write winget manifests from the built setup")
    parser.add_argument("--skip-app", action="store_true",
                        help="reuse dist/VibeForma (the setup only)")
    args = parser.parse_args(argv)

    version = read_version()
    cert = certificate_config()
    if args.sign and not signing_configured(cert):
        print("build_installer: --sign needs VIBEFORMA_CERT_PFX + "
              "VIBEFORMA_CERT_PASSWORD or VIBEFORMA_CERT_THUMBPRINT")
        return 2
    steps = plan(version, args.sign)
    for line in steps:
        print(" -", line)
    if args.dry_run:
        return 0

    env = dict(os.environ)
    if not args.skip_app:
        build_app(version, env)
    else:
        exe = os.path.join(APP_DIST, app_name())
        if not os.path.isfile(exe):
            raise SystemExit(f"no packed application at {exe}")

    iscc = find_iscc()
    if not iscc:
        raise SystemExit("Inno Setup compiler (ISCC.exe) not found")
    setup = build_setup(version, iscc, env)

    signed = False
    if args.sign:
        signed = sign_file(setup, cert)
    else:
        print("build_installer: unsigned build; pass --sign in release CI")

    manifest = installer_manifest(version, setup, signed)
    manifest_path = os.path.join(DIST, "installer-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"setup: {setup}")
    print(f"sha256: {manifest['sha256']}")
    print(f"manifest: {manifest_path}")

    if args.winget:
        documents = winget_documents(version, manifest["sha256"],
                                     manifest["size"], winget_url(version))
        problems = validate_winget_shape(documents)
        if problems:
            for problem in problems:
                print("winget:", problem)
            return 1
        folder = os.path.join(DIST, "winget")
        os.makedirs(folder, exist_ok=True)
        for name, text in documents.items():
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write(text)
        print(f"winget manifests: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
