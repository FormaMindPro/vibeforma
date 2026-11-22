"""Test bootstrap: make the flat app modules importable without installation."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "app"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# pynput needs a display: on Linux its import raises before a test can skip.
collect_ignore = []
if sys.platform != "win32":
    collect_ignore += ["test_cancel.py", "test_hotkey_hold.py"]
