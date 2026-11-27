"""Release-time check: are the shipped models' upstream revisions new?

Before a release, this queries Hugging Face for the current revision (the
commit sha of the repo's main branch) of every model in model_catalog.MODELS
and compares it against tools/model-revisions.json - the baseline recorded
at the previous release check. A changed revision means the release notes
should tell users a newer model exists: a fresh install downloads it anyway,
existing users get the in-app update offer once the app carries the update
check (card-273's feature).

Usage:
  python tools/check_model_revisions.py            compare and report
  python tools/check_model_revisions.py --update   rewrite the baseline
  python tools/check_model_revisions.py --json     machine-readable report

Exit codes: 0 - the baseline matches upstream; 1 - a revision changed or no
baseline exists yet; 2 - the network failed (the check cannot lie offline:
record the stop and retry, never ship a silent "no changes").
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import model_catalog  # noqa: E402

BASELINE = ROOT / "tools" / "model-revisions.json"
API = "https://huggingface.co/api/models/{repo}"
TIMEOUT_S = 20

Fetch = Callable[[str], str]


def fetch(revision_url: str) -> str:
    """The Hugging Face API response body for one model repo."""
    if not revision_url.startswith("https://"):
        raise ValueError(f"only https is allowed: {revision_url}")
    # S310 cannot see the https guard above; the URL is built only from the
    # in-repo model catalog and the fixed API constant.
    request = urllib.request.Request(revision_url, headers={"User-Agent": "vibeforma-release-check"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
        return response.read().decode("utf-8")


def upstream_revision(repo: str, fetch_fn: Fetch) -> str:
    """The current main-branch sha of one repo, or an empty string offline."""
    try:
        payload = json.loads(fetch_fn(API.format(repo=repo)))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"offline: {repo}: {exc}", file=sys.stderr)
        return ""
    return str(payload.get("sha") or "")


def compare(baseline: dict[str, str], revisions: dict[str, str]) -> list[str]:
    """Model names whose upstream revision differs from the baseline."""
    return sorted(name for name, sha in revisions.items()
                  if sha and baseline.get(name) != sha)


def read_baseline(path: Path) -> dict[str, str]:
    """The recorded baseline, or an empty dict when the file does not exist."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items() if v}


def write_baseline(path: Path, revisions: dict[str, str]) -> None:
    """Record the revisions seen by this check."""
    path.write_text(json.dumps(revisions, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="rewrite the baseline with what this check sees")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--baseline", default=str(BASELINE), help="baseline file path")
    args = parser.parse_args(argv)

    baseline = read_baseline(Path(args.baseline))
    revisions: dict[str, str] = {}
    for name in model_catalog.MODELS:
        revisions[name] = upstream_revision(model_catalog.repo_id(name), fetch)

    changed = compare(baseline, revisions)
    offline = [name for name, sha in revisions.items() if not sha]

    if args.json:
        print(json.dumps({
            "revisions": revisions,
            "baseline": baseline,
            "changed": changed,
            "offline": offline,
            "exit": 2 if offline else (1 if changed else 0),
        }, indent=2, sort_keys=True))
    else:
        for name in model_catalog.MODELS:
            sha = revisions[name]
            state = "offline" if not sha else ("changed" if name in changed else "same")
            old = baseline.get(name, "-")
            print(f"{name}: {state} (was {old[:12] if old != '-' else '-'}, now {sha[:12] or 'unknown'})")
        if offline:
            print("the network failed for some models: record the stop and retry - do not ship a silent check")

    if args.update and not offline:
        write_baseline(Path(args.baseline), revisions)
        print(f"baseline recorded: {args.baseline}")
    elif args.update and offline:
        print("baseline not updated: the check went offline", file=sys.stderr)

    if offline:
        return 2
    if changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
