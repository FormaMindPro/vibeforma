#!/usr/bin/env python
"""Check app/config.py VERSION against an expected value (exit 0/1)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_version() -> str | None:
    text = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_version.py <expected-version>")
        return 2
    actual = read_version()
    if actual is None:
        print("check_version: VERSION not found in app/config.py")
        return 1
    if actual != argv[1]:
        print(f"check_version: config has {actual}, expected {argv[1]}")
        return 1
    print(f"check_version: OK {actual}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
