"""Guard: the repository is English-only outside localization resources.

Code, comments, logs, the changelog and commit messages are English. Cyrillic
is allowed only in the UI localization resources, the installer's Russian
texts, the ru/uk ASR prompts, the Russian README and the tests that assert
those localizations.

The full-history scan runs only with VIBEFORMA_CHECK_HISTORY=1, so the normal
suite stays green.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CYRILLIC = re.compile("[\u0400-\u04FF]")
COMMIT_HEADER = re.compile(r"^([0-9a-f]{40})\t", re.MULTILINE)

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".txt",
    ".cmd",
    ".ps1",
    ".yml",
    ".yaml",
    ".in",
    ".iss",
    ".spec",
    ".toml",
    ".cfg",
    ".ini",
}

ALLOWED_DIRS = ("app/locales/",)

ALLOWED_FILES = {
    "README.ru.md",
    "app/asr.py",
    "install_env.py",
    "tests/test_errors.py",
    "tests/test_history_table.py",
    "tests/test_installer_language.py",
    "tests/wer/manifest.json",
}

SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "dist", "models", "logs"}

HISTORY_CHECK = os.environ.get("VIBEFORMA_CHECK_HISTORY") == "1"


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run a git command; None when git is unavailable or fails to start."""
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _walk_paths() -> list[str]:
    """Every candidate text file below the repository root, as posix paths."""
    found: list[str] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            found.append((Path(root) / name).relative_to(REPO_ROOT).as_posix())
    return found


def _text_files() -> list[str]:
    """Tracked text files with a checked suffix; os.walk when git is absent."""
    result = _git("ls-files")
    if result is not None and result.returncode == 0 and result.stdout.strip():
        paths = result.stdout.splitlines()
    else:
        paths = _walk_paths()
    return [path for path in paths if Path(path).suffix.lower() in TEXT_SUFFIXES]


def _is_allowed(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    if normalized in ALLOWED_FILES:
        return True
    return any(normalized.startswith(prefix) for prefix in ALLOWED_DIRS)


def _cyrillic_lines(relative: str) -> list[tuple[int, str]]:
    path = REPO_ROOT / relative
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if CYRILLIC.search(line):
            hits.append((number, line.strip()))
    return hits


def test_tracked_text_files_are_english_only() -> None:
    offenders: list[str] = []
    for relative in sorted(_text_files()):
        if _is_allowed(relative):
            continue
        for number, line in _cyrillic_lines(relative):
            offenders.append(f"{relative}:{number}: {line[:100]}")
    assert not offenders, (
        "Cyrillic outside localization resources:\n" + "\n".join(offenders)
    )


def test_head_commit_message_is_english() -> None:
    result = _git("log", "-1", "--format=%B")
    if result is None or result.returncode != 0:
        pytest.skip("git is unavailable or this directory is not a repository")
    assert not CYRILLIC.search(result.stdout), (
        "HEAD commit message contains Cyrillic:\n" + result.stdout
    )


@pytest.mark.skipif(
    not HISTORY_CHECK,
    reason="set VIBEFORMA_CHECK_HISTORY=1 to scan the whole history",
)
def test_whole_history_is_english() -> None:
    result = _git("log", "--format=%H%x09%s%x09%b")
    if result is None or result.returncode != 0:
        pytest.skip("git is unavailable or this directory is not a repository")
    records = list(COMMIT_HEADER.finditer(result.stdout))
    offenders: list[str] = []
    for index, match in enumerate(records):
        end = records[index + 1].start() if index + 1 < len(records) else len(result.stdout)
        if CYRILLIC.search(result.stdout[match.end() : end]):
            offenders.append(match.group(1))
    assert not offenders, (
        f"Cyrillic in {len(offenders)} commit message(s): " + ", ".join(offenders)
    )
