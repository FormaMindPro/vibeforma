"""T11: the packed build, the per-user setup, signing and winget manifests."""
import hashlib
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "vibeforma.spec"
ISS = ROOT / "packaging" / "installer.iss"
ENTRY = ROOT / "packaging" / "entrypoint.py"


def load_tool(rel):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bi = load_tool("tools/build_installer.py")
br = load_tool("tools/build_release.py")


def test_version_and_names():
    version = bi.read_version()
    assert version == br.read_version()
    assert bi.setup_name(version) == f"VibeForma-Setup-{version}.exe"
    assert bi.app_name() == "VibeForma.exe"
    assert bi.version_tuple("1.2.3") == (1, 2, 3)


def test_version_resource_carries_the_version():
    text = bi.version_resource("1.2.3")
    assert "filevers=(1, 2, 3, 0)" in text
    assert "ProductVersion" in text and "FormaMindPro" in text
    assert "VibeForma.exe" in text


def test_signing_is_fail_closed(monkeypatch):
    for name in ("VIBEFORMA_CERT_PFX", "VIBEFORMA_CERT_PASSWORD",
                 "VIBEFORMA_CERT_THUMBPRINT"):
        monkeypatch.delenv(name, raising=False)
    cert = bi.certificate_config()
    assert not bi.signing_configured(cert)
    with pytest.raises(ValueError):
        bi.sign_args("signtool", "a.exe", cert)


def test_signing_arguments(monkeypatch):
    monkeypatch.setenv("VIBEFORMA_CERT_THUMBPRINT", "ABC123")
    args = bi.sign_args("signtool", "setup.exe", bi.certificate_config())
    assert "/sha1" in args and "ABC123" in args
    assert args[-1] == "setup.exe"
    assert "/fd" in args and "sha256" in args

    monkeypatch.delenv("VIBEFORMA_CERT_THUMBPRINT")
    monkeypatch.setenv("VIBEFORMA_CERT_PFX", "cert.pfx")
    monkeypatch.setenv("VIBEFORMA_CERT_PASSWORD", "secret")
    args = bi.sign_args("signtool", "setup.exe", bi.certificate_config())
    assert "/f" in args and "cert.pfx" in args
    assert "/p" in args and "secret" in args


def test_installer_manifest_hashes_the_file(tmp_path):
    setup = tmp_path / "VibeForma-Setup-1.2.3.exe"
    setup.write_bytes(b"setup")
    manifest = bi.installer_manifest("1.2.3", str(setup), signed=True)
    assert manifest["installer"] == setup.name
    assert manifest["sha256"] == hashlib.sha256(b"setup").hexdigest()
    assert manifest["size"] == 5 and manifest["signed"] is True


def test_winget_documents_are_complete():
    docs = bi.winget_documents(
        "1.2.3", "ab" * 32, 1234,
        "https://github.com/FormaMindPro/vibeforma/releases/download/"
        "v1.2.3/VibeForma-Setup-1.2.3.exe")
    assert set(docs) == {
        "FormaMindPro.VibeForma.yaml",
        "FormaMindPro.VibeForma.installer.yaml",
        "FormaMindPro.VibeForma.locale.en-US.yaml",
        "FormaMindPro.VibeForma.locale.ru-RU.yaml",
    }
    assert bi.validate_winget_shape(docs) == []
    installer = docs["FormaMindPro.VibeForma.installer.yaml"]
    assert "InstallerSha256: " + "AB" * 32 in installer
    assert "InstallerType: inno" in installer
    broken = dict(docs)
    broken["FormaMindPro.VibeForma.installer.yaml"] = installer.replace(
        "AB" * 32, "xyz")
    assert bi.validate_winget_shape(broken)


def test_spec_is_one_folder_without_models():
    text = SPEC.read_text(encoding="utf-8")
    assert "COLLECT(" in text
    assert 'contents_directory="_internal"' in text
    assert 'name="VibeForma"' in text
    assert "collect_data_files" in text
    assert "\"models\"" not in text
    assert "excludes=" in text and "pytest" in text


def test_setup_is_per_user_and_bilingual():
    text = ISS.read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in text
    assert "DefaultDirName={localappdata}\\Programs\\VibeForma" in text
    assert 'Name: "ru"' in text and "Russian.isl" in text
    assert "UninstallDisplayName=VibeForma" in text
    assert "CloseApplications=yes" in text
    assert "UninstallDelete" in text and "{localappdata}\\VibeForma" in text
    assert "desktopicon" in text and "userstartup" in text


def test_entrypoint_keeps_data_out_of_the_program_folder():
    text = ENTRY.read_text(encoding="utf-8")
    assert "VIBEFORMA_DATA_DIR" in text
    assert "LOCALAPPDATA" in text
    assert text.index("VIBEFORMA_DATA_DIR") < text.index("from vibeforma")


def test_release_workflow_signs_and_fails_closed():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "tools/build_installer.py --sign --winget" in text
    assert "WINDOWS_CERT_PFX_BASE64 is not configured" in text
    assert "winget validate dist/winget" in text
    assert "--installer" in text
    assert "installer:" in text and "needs: installer" in text


def test_packed_build_refuses_the_zip_updater(monkeypatch):
    import updater
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    release = {"tag": "v1.2.3", "zip": "x", "manifest": "m",
               "signature": "s"}
    ok, reason = updater.apply(release, str(ROOT), logging.getLogger("test"))
    assert not ok
    assert "installer" in reason


def test_build_release_mentions_the_installer(tmp_path):
    version = bi.read_version()
    setup = tmp_path / f"VibeForma-Setup-{version}.exe"
    setup.write_bytes(b"setup")
    entry = br.installer_entry(str(setup), version)
    assert entry["name"] == setup.name
    assert entry["sha256"] == hashlib.sha256(b"setup").hexdigest()
    with pytest.raises(SystemExit):
        br.installer_entry(str(tmp_path / "other.exe"), version)


def test_data_dir_override_is_honoured(tmp_path):
    env = dict(os.environ)
    env["VIBEFORMA_DATA_DIR"] = str(tmp_path / "data")
    env["PYTHONPATH"] = str(ROOT / "app")
    code = ("import config; print(config.CONFIG_PATH); "
            "print(config.MODELS_DIR); print(config.LOG_DIR)")
    out = subprocess.run(  # noqa: S603 - a fixed interpreter and code string
        [sys.executable, "-c", code], env=env, text=True,
        capture_output=True, cwd=str(ROOT), check=True).stdout
    lines = out.strip().splitlines()
    assert lines[0] == str(tmp_path / "data" / "config.json")
    assert lines[1] == str(tmp_path / "data" / "models")
    assert lines[2] == str(tmp_path / "data" / "logs")
