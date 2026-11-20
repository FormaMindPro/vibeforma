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
        "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"},
            {"name": "VibeForma-0.2.0.zip", "browser_download_url": "https://x/z"},
        ],
    }))

    release = updater.latest_release()

    assert release["tag"] == "v0.2.0"
    assert release["version"] == (0, 2, 0)
    assert release["zip"] == "https://x/z"


def test_latest_release_without_zip_asset(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", _fake_urlopen({
        "tag_name": "v0.2.0",
        "assets": [],
    }))

    assert updater.latest_release()["zip"] is None
