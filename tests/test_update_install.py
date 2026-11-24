"""install.ps1 with a local release zip: the hash gate runs before unpacking.

Windows-only. The test builds a tiny release zip whose install.py records
its argv, so no network and no real installer is involved.
"""
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.ps1"

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(os.name != "nt", reason="install.ps1 is Windows-only"),
]

FAKE_INSTALL = """import json
import os
import sys

path = os.environ.get("VIBEFORMA_TEST_RAN")
if path:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sys.argv[1:], handle)
sys.exit(0)
"""


def _release(tmp_path):
    source = tmp_path / "release" / "VibeForma-test"
    source.mkdir(parents=True)
    (source / "install.py").write_text(FAKE_INSTALL, encoding="ascii")
    archive = tmp_path / "VibeForma-test.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(source / "install.py", "VibeForma-test/install.py")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return archive, sums, digest


def _run(tmp_path, archive, sums, *, with_sums=True):
    target = tmp_path / "target"
    marker = tmp_path / "ran.json"
    env = os.environ.copy()
    env["VIBEFORMA_TEST_RAN"] = str(marker)
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(SCRIPT), "-ZipPath", str(archive),
        "-Target", str(target), "-NoRun",
    ]
    if with_sums:
        command[8:8] = ["-SumsPath", str(sums)]
    result = subprocess.run(  # noqa: S603 - fixed argv, local script
        command, capture_output=True, text=True, env=env, timeout=120)
    return result, target, marker


def test_verified_local_zip_reaches_install_py(tmp_path):
    archive, sums, _digest = _release(tmp_path)

    result, target, marker = _run(tmp_path, archive, sums)

    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(marker.read_text(encoding="utf-8"))
    assert "--target" in args
    assert str(target) in args
    assert "--no-run" in args


def test_wrong_hash_stops_before_unpacking(tmp_path):
    archive, sums, _digest = _release(tmp_path)
    sums.write_text("0" * 64 + f"  {archive.name}\n", encoding="ascii")

    result, _target, marker = _run(tmp_path, archive, sums)

    assert result.returncode == 1
    assert not marker.exists()
    assert "SHA256 mismatch" in result.stdout


def test_missing_sums_line_stops_before_unpacking(tmp_path):
    archive, sums, digest = _release(tmp_path)
    sums.write_text(digest + "  another.zip\n", encoding="ascii")

    result, _target, marker = _run(tmp_path, archive, sums)

    assert result.returncode == 1
    assert not marker.exists()
    assert "no line" in result.stdout


def test_zip_without_sums_stops(tmp_path):
    archive, sums, _digest = _release(tmp_path)

    result, _target, marker = _run(tmp_path, archive, sums, with_sums=False)

    assert result.returncode == 1
    assert not marker.exists()
    assert "-SumsPath" in result.stdout
