"""Updater integrity and rollback: only a verified release changes files."""
import json
import pathlib
import shutil
import zipfile

import ed25519_testkey as fixture
import release_verify as rv
import updater

TAG = "v0.1.9"
ROOT = "VibeForma-0.1.9"
REQUIRED = ("install.py", "app/main.py", "app/config.py", "app/run.cmd",
            "test_ru.wav")


def build_release(tmp_path):
    members = {name: ("def main():\n    pass\n" if name.endswith(".py") else "x")
               for name in REQUIRED}
    members["README.md"] = "readme"
    zip_path = tmp_path / "VibeForma-0.1.9.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, body in members.items():
            archive.writestr(ROOT + "/" + name, body)
    manifest = {
        "version": "0.1.9",
        "zip": zip_path.name,
        "sha256": rv.sha256_file(zip_path),
        "size": zip_path.stat().st_size,
        "files": sorted(ROOT + "/" + name for name in members),
    }
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    seed, public_key, _pub = fixture.keypair()
    sig_path = tmp_path / "release-manifest.json.minisig"
    sig_path.write_text(
        fixture.signature_file(seed, manifest_path.read_bytes()),
        encoding="utf-8")
    release = {
        "tag": TAG,
        "version": (0, 1, 9),
        "zip": "https://test/zip",
        "manifest": "https://test/manifest",
        "signature": "https://test/signature",
        "html": "https://test/release",
    }
    sources = {
        release["zip"]: zip_path,
        release["manifest"]: manifest_path,
        release["signature"]: sig_path,
    }
    return release, sources, public_key


def service_dir(tmp_path):
    svc = tmp_path / "service"
    (svc / "assets").mkdir(parents=True)
    (svc / "models").mkdir()
    (svc / "logs").mkdir()
    (svc / "main.py").write_text("old main\n", encoding="utf-8")
    (svc / "config.json").write_text('{"model": "small"}', encoding="utf-8")
    (svc / "assets" / "icon.txt").write_text("old icon", encoding="utf-8")
    (svc / "models" / "model.bin").write_text("keep model", encoding="utf-8")
    (svc / "logs" / "service.log").write_text("keep log", encoding="utf-8")
    return svc


def fake_download(sources):
    def download(url, path, timeout):
        shutil.copy2(sources[url], path)
    return download


def test_latest_release_reports_manifest_and_signature(monkeypatch):
    payload = {
        "tag_name": "v0.2.0",
        "assets": [
            {"name": "release-manifest.json", "browser_download_url": "https://x/m"},
            {"name": "release-manifest.json.minisig",
             "browser_download_url": "https://x/s"},
            {"name": "VibeForma-0.2.0.zip", "browser_download_url": "https://x/z"},
        ],
    }

    class Response:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda _req, timeout=0: Response())
    release = updater.latest_release()
    assert release["manifest"] == "https://x/m"
    assert release["signature"] == "https://x/s"
    assert release["zip"] == "https://x/z"


def test_apply_without_a_trust_anchor_is_refused(tmp_path):
    release = {"tag": TAG, "zip": "https://x/z"}
    ok, reason = updater.apply(release, str(tmp_path), _log())
    assert not ok
    assert "no manifest" in reason or "not configured" in reason


def test_apply_refuses_a_tampered_zip_and_leaves_files_alone(
        tmp_path, monkeypatch, caplog):
    release, sources, key = build_release(tmp_path)
    zip_path = sources[release["zip"]]
    with open(zip_path, "ab") as f:
        f.write(b"tampered")
    svc = service_dir(tmp_path)
    monkeypatch.setattr(updater, "download_asset", fake_download(sources))
    ok, reason = updater.apply(
        release, str(svc), _log(), installer=_never_installer,
        public_key_b64=key)
    assert not ok
    assert "hash" in reason
    assert (svc / "main.py").read_text(encoding="utf-8") == "old main\n"


def test_apply_refuses_a_tampered_manifest(tmp_path, monkeypatch):
    release, sources, key = build_release(tmp_path)
    manifest_path = sources[release["manifest"]]
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("0.1.9", "0.1.8"),
        encoding="utf-8")
    svc = service_dir(tmp_path)
    monkeypatch.setattr(updater, "download_asset", fake_download(sources))
    ok, reason = updater.apply(
        release, str(svc), _log(), installer=_never_installer,
        public_key_b64=key)
    assert not ok
    assert "signature" in reason


def test_failed_installer_restores_the_previous_version(
        tmp_path, monkeypatch, caplog):
    release, sources, key = build_release(tmp_path)
    svc = service_dir(tmp_path)
    monkeypatch.setattr(updater, "download_asset", fake_download(sources))

    def failing_installer(_src, service):
        service = pathlib.Path(service)
        (service / "main.py").write_text("broken\n", encoding="utf-8")
        (service / "new_module.py").write_text("half written\n", encoding="utf-8")
        (service / "assets" / "new.txt").write_text("half written\n", encoding="utf-8")
        return 1, "boom"

    ok, reason = updater.apply(
        release, str(svc), _log(), installer=failing_installer,
        public_key_b64=key)
    assert not ok
    assert "installer exit 1" in reason
    assert (svc / "main.py").read_text(encoding="utf-8") == "old main\n"
    assert not (svc / "new_module.py").exists()
    assert not (svc / "assets" / "new.txt").exists()
    assert (svc / "assets" / "icon.txt").read_text(encoding="utf-8") == "old icon"
    assert (svc / "config.json").read_text(encoding="utf-8") == '{"model": "small"}'
    assert (svc / "models" / "model.bin").exists()
    assert (svc / "logs" / "service.log").exists()
    assert "previous files restored" in caplog.text


def test_successful_install_keeps_the_new_files(tmp_path, monkeypatch):
    release, sources, key = build_release(tmp_path)
    svc = service_dir(tmp_path)
    monkeypatch.setattr(updater, "download_asset", fake_download(sources))

    def installer(_src, service):
        service = pathlib.Path(service)
        (service / "main.py").write_text("new main\n", encoding="utf-8")
        return 0, ""

    ok, reason = updater.apply(
        release, str(svc), _log(), installer=installer,
        public_key_b64=key)
    assert ok
    assert reason == ""
    assert (svc / "main.py").read_text(encoding="utf-8") == "new main\n"
    assert (svc / "models" / "model.bin").exists()
    assert (svc / "logs" / "service.log").exists()


def test_backup_and_restore_do_not_touch_user_data(tmp_path):
    svc = service_dir(tmp_path)
    backup = tmp_path / "backup"
    names = updater.backup_service(str(svc), str(backup))
    (svc / "main.py").write_text("changed\n", encoding="utf-8")
    (svc / "extra.py").write_text("extra\n", encoding="utf-8")
    updater.restore_service(str(svc), str(backup), names)
    assert (svc / "main.py").read_text(encoding="utf-8") == "old main\n"
    assert not (svc / "extra.py").exists()
    assert (svc / "models" / "model.bin").read_text(encoding="utf-8") == "keep model"
    assert (svc / "logs" / "service.log").read_text(encoding="utf-8") == "keep log"


def _never_installer(_src, _service):
    raise AssertionError("installer must not run")


def _log():
    import logging
    logger = logging.getLogger("test-updater")
    logger.addHandler(logging.NullHandler())
    return logger
