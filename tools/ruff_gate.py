#!/usr/bin/env python
"""Ruff baseline gate: fail only on findings absent from the recorded baseline.

The product started with 164 ruff findings. Fixing all of them is a separate
task (T10); until then the gate keeps the current count as a ceiling and fails
when a change adds a new finding. Fewer findings are always allowed.

Run:
  python tools/ruff_gate.py            # check against tools/ruff-baseline.json
  python tools/ruff_gate.py --update   # rewrite the baseline (review the diff)
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(ROOT, "tools", "ruff-baseline.json")
TARGETS = ["app", "install.py", "tests", "tools"]
SELECT = "E4,E7,E9,F,B,SIM,UP,I,S,BLE"


def run_ruff():
    """Current findings as JSON. Exit 2 when ruff itself is unavailable."""
    cmd = [
        sys.executable, "-m", "ruff", "check",
        "--no-cache",
        "--select", SELECT,
        "--output-format", "json",
        *TARGETS,
    ]
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr or proc.stdout)
        return None
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        sys.stderr.write(proc.stdout)
        return None


def _normalise(filename):
    path = filename
    if os.path.isabs(path):
        try:
            path = os.path.relpath(path, ROOT)
        except ValueError:
            path = os.path.basename(path)
    return os.path.normpath(path).replace("\\", "/")


def count_by_key(findings):
    counts = {}
    for item in findings:
        key = f"{_normalise(item['filename'])}|{item.get('code') or '?'}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def write_baseline(counts):
    payload = {
        "tool": "ruff",
        "select": SELECT,
        "targets": TARGETS,
        "total": sum(counts.values()),
        "findings": dict(sorted(counts.items())),
    }
    with open(BASELINE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true",
                        help="rewrite the baseline from the current findings")
    args = parser.parse_args()

    findings = run_ruff()
    if findings is None:
        print("ruff gate: ruff is not available; install requirements-dev.txt")
        return 2
    current = count_by_key(findings)

    if args.update:
        write_baseline(current)
        print(f"ruff gate: baseline updated, {sum(current.values())} findings")
        return 0

    try:
        with open(BASELINE_PATH, encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"ruff gate: cannot read {BASELINE_PATH}: {exc}")
        return 2
    allowed = baseline.get("findings", {})

    new_findings = []
    for key, count in sorted(current.items()):
        extra = count - int(allowed.get(key, 0))
        if extra > 0:
            new_findings.append((key, extra))

    print(f"ruff gate: {sum(current.values())} findings now, "
          f"{sum(int(v) for v in allowed.values())} in baseline")
    if new_findings:
        print(f"NEW FINDINGS ({sum(extra for _, extra in new_findings)}):")
        for key, extra in new_findings:
            print(f"  +{extra}  {key}")
        return 1
    print("ruff gate: no new findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
