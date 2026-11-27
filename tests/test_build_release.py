"""The app/ whitelist gate in tools/build_release.py.

Personal data of a development install lives next to the code (config
json, history, locks). The builder keeps a known set of such runtime
files: they are excluded from the archive loudly, without failing the
build. Anything else found in app/ outside the whitelist must fail the
build instead of being skipped silently or, worse, shipped. The module
is loaded by path (tools/ is not a package) and its ROOT/APP globals
are pointed at a synthetic tree under tmp_path, so the repository is
never written to and no dist/ output is produced.
"""
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "build_release.py")


def load_module():
    spec = importlib.util.spec_from_file_location("build_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_module()


def make_tree(root, extra=()):
    """Lay out a minimal buildable release tree under `root`."""
    for name in release.TOP_FILES:
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("placeholder\n")
    for rel in release.APP_FILES:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("placeholder\n")
    with open(os.path.join(root, "app", "config.py"), "w",
              encoding="utf-8") as f:
        f.write('VERSION = "0.5.2"\n')
    with open(os.path.join(root, "app", "main.py"), "w",
              encoding="utf-8") as f:
        f.write('if "--selftest" in sys.argv:\n    pass\n')
    for rel in extra:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("personal data\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    make_tree(str(tmp_path))
    monkeypatch.setattr(release, "ROOT", str(tmp_path))
    monkeypatch.setattr(release, "APP", os.path.join(str(tmp_path), "app"))
    return tmp_path


def test_std_set_passes_collect(tree):
    files = release.collect("0.5.2")
    assert set(files) == set(release.TOP_FILES) | set(release.APP_FILES)
    assert "app/main.py" in files


def test_check_passes_on_the_std_set(tree, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [SCRIPT, "--check"])
    assert release.main() == 0
    out = capsys.readouterr().out
    assert "inputs OK" in out


PERSONAL_RELATIVES = [
    "config.json",
    "stats.json",
    "history.jsonl",
    "corrections.json",
    "service.lock",
]

PERSONAL_FILES = ["app/" + name for name in PERSONAL_RELATIVES]
PERSONAL_FILES.append("app/history_audio/note.wav")
PERSONAL_FILES.append("app/history_audio/deep/take.wav")


def test_personal_files_are_excluded_not_shipped(tree, capsys):
    make_tree(str(tree), extra=tuple(PERSONAL_FILES))
    files = release.collect("0.5.2")
    for rel in PERSONAL_FILES:
        assert rel not in files
    out = capsys.readouterr().out
    assert "build: excluded personal files:" in out
    for rel in PERSONAL_FILES:
        assert rel in out


def test_check_passes_with_personal_files(tree, monkeypatch, capsys):
    make_tree(str(tree), extra=("app/stats.json", "app/history_audio/a.wav"))
    monkeypatch.setattr(sys, "argv", [SCRIPT, "--check"])
    assert release.main() == 0
    out = capsys.readouterr().out
    assert "build: excluded personal files: app/history_audio/a.wav, " \
        "app/stats.json" in out
    assert "inputs OK" in out


def test_check_passes_with_personal_files_as_a_subprocess(tmp_path):
    make_tree(str(tmp_path), extra=("app/history.jsonl",))
    script = tmp_path / "tools" / "build_release.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, str(script))
    proc = subprocess.run([sys.executable, str(script), "--check"],  # noqa: S603 - fixed script built inside the tmp dir
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert "build: excluded personal files: app/history.jsonl" in proc.stdout


UNKNOWN_FILES = [
    "app/scratch.txt",
    "app/vibeforma/leftover.py",
    "app/history.db",
]


@pytest.mark.parametrize("rel", UNKNOWN_FILES)
def test_unexpected_file_fails_collect(tree, rel):
    make_tree(str(tree), extra=(rel,))
    with pytest.raises(SystemExit) as excinfo:
        release.collect("0.5.2")
    assert rel in str(excinfo.value)
    assert "whitelist" in str(excinfo.value)


def test_check_fails_loud_as_a_subprocess(tmp_path):
    make_tree(str(tmp_path), extra=("app/scratch.txt",))
    script = tmp_path / "tools" / "build_release.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, str(script))
    proc = subprocess.run([sys.executable, str(script), "--check"],  # noqa: S603 - fixed script built inside the tmp dir
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert "outside the app/ whitelist" in proc.stderr


def test_whitelist_matches_the_committed_app_tree():
    """The whitelist must equal the committed app/ inventory.

    A git-aware comparison keeps this stable on a live development tree:
    personal runtime files are never committed, so `git ls-files app/`
    reports exactly what is supposed to ship. The test only reads the
    repository.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not available on PATH")
    proc = subprocess.run(["git", "ls-files", "app/"],  # noqa: S607 - git resolved from PATH on purpose
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0
    committed = set(line.strip().replace("\\", "/")
                    for line in proc.stdout.splitlines() if line.strip())
    assert committed == set(release.APP_FILES)
