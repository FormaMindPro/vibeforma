"""Release verification: Ed25519 vector, manifest signature, zip safety."""
import base64
import json
import zipfile

import ed25519_testkey as fixture
import pytest
import release_verify as rv

TAG = "v0.1.9"
ROOT = "VibeForma-0.1.9"
REQUIRED = ("install.py", "app/main.py", "app/config.py", "app/run.cmd",
            "test_ru.wav")


def manifest_bytes(files, version="0.1.9", digest="0" * 64):
    return json.dumps({
        "version": version,
        "zip": f"VibeForma-{version}.zip",
        "sha256": digest,
        "files": sorted(files),
    }).encode("utf-8")


def write_zip(path, members, prefix=ROOT + "/", symlink=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members.items():
            archive.writestr(prefix + name, body)
        if symlink:
            info = zipfile.ZipInfo(prefix + symlink)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "target")


def standard_members():
    members = {name: b"x" for name in REQUIRED}
    members["README.md"] = b"readme"
    return members


def test_rfc8032_vector():
    public = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert rv.ed25519_verify(public, b"", signature)
    assert not rv.ed25519_verify(public, b"x", signature)


def test_manifest_signature_roundtrip():
    seed, public_key, _pub = fixture.keypair()
    message = manifest_bytes([ROOT + "/" + n for n in REQUIRED])
    sig = fixture.signature_file(seed, message)
    assert rv.verify_manifest(message, sig, public_key)


def test_prehashed_signature_is_accepted():
    seed, public_key, _pub = fixture.keypair()
    message = b'{"version": "0.1.9"}'
    sig = fixture.signature_file(seed, message, prehashed=True)
    assert rv.verify_manifest(message, sig, public_key)


def test_tampered_manifest_is_rejected():
    seed, public_key, _pub = fixture.keypair()
    message = b"original"
    sig = fixture.signature_file(seed, message)
    with pytest.raises(rv.ReleaseError, match="signature is invalid"):
        rv.verify_manifest(b"tampered", sig, public_key)


def test_empty_trust_anchor_refuses_every_update():
    seed, _public_key, _pub = fixture.keypair()
    sig = fixture.signature_file(seed, b"x")
    with pytest.raises(rv.ReleaseError, match="not configured"):
        rv.verify_manifest(b"x", sig, "")


def test_signature_from_another_key_id_is_rejected():
    seed, public_key, _pub = fixture.keypair()
    sig = fixture.signature_file(seed, b"x")
    other = b"Ed" + b"OTHERKEY" + b"\x01" * 32
    other_key = base64.b64encode(other).decode("ascii")
    with pytest.raises(rv.ReleaseError, match="different key"):
        rv.verify_manifest(b"x", sig, other_key)


def test_parse_manifest_accepts_a_complete_document():
    files = [ROOT + "/" + name for name in REQUIRED]
    parsed = rv.parse_manifest(manifest_bytes(files))
    assert parsed["version"] == "0.1.9"
    assert parsed["zip"] == "VibeForma-0.1.9.zip"
    assert parsed["files"] == sorted(files)


@pytest.mark.parametrize("digest", ["", "abc", "z" * 64])
def test_parse_manifest_rejects_a_bad_digest(digest):
    with pytest.raises(rv.ReleaseError, match="sha256"):
        rv.parse_manifest(manifest_bytes([ROOT + "/install.py"], digest=digest))


@pytest.mark.parametrize("name", [
    "../evil", "/absolute", "C:/evil", "a\\b", "a//b", "a/./b", "",
])
def test_parse_manifest_rejects_unsafe_paths(name):
    with pytest.raises(rv.ReleaseError):
        rv.parse_manifest(manifest_bytes([name]))


def test_verify_zip_accepts_a_complete_archive(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members)
    manifest = {"sha256": rv.sha256_file(zip_path),
                "files": sorted(ROOT + "/" + n for n in members)}
    assert rv.verify_zip(zip_path, manifest) == ROOT


def test_verify_zip_rejects_a_hash_mismatch(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members)
    manifest = {"sha256": "0" * 64,
                "files": sorted(ROOT + "/" + n for n in members)}
    with pytest.raises(rv.ReleaseError, match="hash"):
        rv.verify_zip(zip_path, manifest)


def test_verify_zip_rejects_an_unknown_file(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members)
    listed = sorted(ROOT + "/" + n for n in REQUIRED)
    manifest = {"sha256": rv.sha256_file(zip_path), "files": listed}
    with pytest.raises(rv.ReleaseError, match="not in the manifest"):
        rv.verify_zip(zip_path, manifest)


def test_verify_zip_rejects_a_missing_required_file(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    del members["test_ru.wav"]
    write_zip(zip_path, members)
    manifest = {"sha256": rv.sha256_file(zip_path),
                "files": sorted(ROOT + "/" + n for n in members)}
    with pytest.raises(rv.ReleaseError, match="missing required"):
        rv.verify_zip(zip_path, manifest)


def test_verify_zip_rejects_traversal(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members)
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("../evil.py", "x")
    manifest = {"sha256": rv.sha256_file(zip_path),
                "files": sorted(ROOT + "/" + n for n in members) + ["../evil.py"]}
    with pytest.raises(rv.ReleaseError, match="unsafe path"):
        rv.verify_zip(zip_path, manifest)


def test_verify_zip_rejects_an_absolute_path(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members)
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("/evil.py", "x")
    manifest = {"sha256": rv.sha256_file(zip_path),
                "files": sorted(ROOT + "/" + n for n in members) + ["/evil.py"]}
    with pytest.raises(rv.ReleaseError, match="absolute"):
        rv.verify_zip(zip_path, manifest)


def test_verify_zip_rejects_a_symlink(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members, symlink="link")
    manifest = {"sha256": rv.sha256_file(zip_path),
                "files": sorted(ROOT + "/" + n for n in members)
                + [ROOT + "/link"]}
    with pytest.raises(rv.ReleaseError, match="symbolic link"):
        rv.verify_zip(zip_path, manifest)


def test_extract_verified_extracts_only_listed_members(tmp_path):
    zip_path = tmp_path / "release.zip"
    members = standard_members()
    write_zip(zip_path, members)
    manifest = {"sha256": rv.sha256_file(zip_path),
                "files": sorted(ROOT + "/" + n for n in members)}
    src = rv.extract_verified(zip_path, manifest, tmp_path / "x")
    assert (tmp_path / "x" / ROOT / "install.py").exists()
    assert src.endswith(ROOT)


def test_version_matches_tag():
    with pytest.raises(rv.ReleaseError, match="does not match"):
        rv.version_matches({"version": "0.1.9"}, "v0.2.0")
    assert rv.version_matches({"version": "0.1.9"}, "v0.1.9")
