"""Test bootstrap: make the flat app modules importable without installation."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "app")):
    if entry not in sys.path:
        sys.path.insert(0, entry)
