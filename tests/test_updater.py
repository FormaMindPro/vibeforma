"""Updater pure logic: version parsing, comparison, release parsing offline."""
import hashlib
import json

import pytest
import updater


@pytest.mark.parametrize("raw,expected", [
    ("v0.1.3", (0, 1, 3)),
    ("0.1.3", (0, 1, 3)),
    ("V2.0", (2, 0)),
    ("garbage", None),
    ("1.x.3", None),
    (None, None),
])
def test_version_tuple(raw, expected):
    assert updater.version_tuple(raw) == expected


def test_is_newer():
    assert updater.is_newer((0, 2, 0), (0, 1, 9))
    assert not updater.is_newer((0, 1, 9), (0, 1, 9))
    assert not updater.is_newer((0, 1, 8), (0, 1, 9))


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload):
    body = json.dumps(payload).encode("utf-8")

    def urlopen(_request, timeout=0):
        return _Response(body)

    return urlopen


def test_latest_release_picks_the_zip_asset(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", _fake_urlopen({
        "tag_name": "v0.2.0",
        "html_url": "https://example.invalid/release",
        "body": "## Added\n- Doctor",
        "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"},
            {"name": "VibeForma-0.2.0.zip", "browser_download_url": "https://x/z"},
        ],
    }))

    release = updater.latest_release()

    assert release["tag"] == "v0.2.0"
    assert release["version"] == (0, 2, 0)
    assert release["zip"] == "https://x/z"
    assert release["sums"] == "https://x/sums"
    assert release["body"] == "## Added\n- Doctor"


def test_release_notes_url_needs_https():
    assert updater.release_notes_url(
        {"html": "https://example.invalid/r"}) == "https://example.invalid/r"
    assert updater.release_notes_url({"html": "http://example.invalid/r"}) == ""
    assert updater.release_notes_url({"html": ""}) == ""
    assert updater.release_notes_url(None) == ""


def test_changelog_excerpt_is_plain_and_bounded():
    body = ("## Added\n\n- **Doctor** report with `--doctor`\n"
            "- [Link](https://x)\n\nFull Changelog: https://x/compare")
    excerpt = updater.changelog_excerpt(body, limit=30)
    assert "**" not in excerpt and "`" not in excerpt
    assert "##" not in excerpt
    assert "Full Changelog" not in excerpt
    assert "Doctor" in excerpt
    assert "](" not in excerpt
    assert len(excerpt) <= 30
    assert excerpt.endswith("...")


def test_changelog_excerpt_of_nothing_is_empty():
    assert updater.changelog_excerpt(None) == ""
    assert updater.changelog_excerpt("") == ""


def test_latest_release_without_zip_asset(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", _fake_urlopen({
        "tag_name": "v0.2.0",
        "assets": [],
    }))

    release = updater.latest_release()
    assert release["zip"] is None
    assert release["sums"] is None


def test_parse_sha256sums_reads_lines_and_skips_garbage():
    digest = "a" * 64
    text = (digest + "  VibeForma-0.4.4.zip\n"
            + digest.upper() + " *VibeForma-0.4.4.zip.sig\n"
            + "\n"
            + "not-a-hash  file.zip\n"
            + "b" * 64 + "\n")

    assert updater.parse_sha256sums(text) == {
        "VibeForma-0.4.4.zip": digest,
        "VibeForma-0.4.4.zip.sig": digest,
    }
    assert updater.parse_sha256sums(None) == {}


def test_expected_sha256_is_lower_case_and_name_bound():
    digest = "c" * 64
    text = digest.upper() + "  VibeForma-0.4.4.zip\n"

    assert updater.expected_sha256(text, "VibeForma-0.4.4.zip") == digest
    assert updater.expected_sha256(text, "other.zip") is None
    assert updater.expected_sha256("", "VibeForma-0.4.4.zip") is None


def test_sha256_file_and_verify(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(b"vibeforma")
    digest = hashlib.sha256(b"vibeforma").hexdigest()

    assert updater.sha256_file(str(path)) == digest
    assert updater.verify_sha256(str(path), digest)
    assert updater.verify_sha256(str(path), digest.upper())
    assert not updater.verify_sha256(str(path), "0" * 64)
    assert not updater.verify_sha256(str(path), "")


def test_download_file_refuses_a_non_https_url(tmp_path):
    with pytest.raises(ValueError):
        updater.download_file("http://example.invalid/x.zip",
                              str(tmp_path / "x.zip"))
    assert not (tmp_path / "x.zip").exists()


def test_is_source_checkout_needs_pyproject_and_no_marker(tmp_path):
    service = tmp_path / "app"
    service.mkdir()

    assert not updater.is_source_checkout(str(service))
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert updater.is_source_checkout(str(service))
    (service / ".vibeforma-install.json").write_text("{}", encoding="utf-8")
    assert not updater.is_source_checkout(str(service))
