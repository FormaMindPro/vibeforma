"""Updater pure logic: version parsing, comparison, release parsing offline."""
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

    assert updater.latest_release()["zip"] is None
