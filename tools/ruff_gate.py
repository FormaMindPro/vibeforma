#!/usr/bin/env python
"""Ruff gate: the repository is clean, and any finding fails the build.

The 0.1.9 release recorded 164 findings as a ceiling while the cleanup was a
separate task (T10). The cleanup is done: the baseline is gone and ruff is
blocking.

Run:
  python tools/ruff_gate.py            # the same selection as CI
  python -m ruff check .               # the plain linter
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELECT = "E4,E7,E9,F,B,SIM,UP,I,S,BLE,RUF100"
TARGETS = ["app", "install.py", "install_core.py", "install_env.py",
           "install_steps.py", "install_system.py", "tests", "tools"]


def main():
    cmd = [sys.executable, "-m", "ruff", "check",
           "--no-cache", "--select", SELECT, *TARGETS]
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
