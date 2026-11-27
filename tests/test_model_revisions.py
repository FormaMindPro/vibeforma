"""The release-time model revision check (card-307).

The mapping parity keeps ``asr._repo_id`` and the release tool on one
reference (model_catalog); the compare/report logic is tested offline with
a stubbed fetch - no network in the suite.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tools"))

import asr  # noqa: E402
import check_model_revisions as cr  # noqa: E402
import model_catalog  # noqa: E402


def test_repo_mapping_lives_in_model_catalog():
    """asr reads the repo mapping from model_catalog - one reference."""
    for name in model_catalog.MODELS:
        assert asr._repo_id(name) == model_catalog.repo_id(name)
    assert asr._repo_id("large-v3-turbo") == "dropbox-dash/faster-whisper-large-v3-turbo"
    assert asr._repo_id("large-v3") == "Systran/faster-whisper-large-v3"
    # A repo id passed through stays a repo id.
    assert asr._repo_id("user/local-ct2") == "user/local-ct2"


def test_upstream_revision_reads_the_api_sha():
    body = json.dumps({"sha": "abc123", "siblings": []})
    assert cr.upstream_revision("Systran/faster-whisper-large-v3", lambda url: body) == "abc123"
    assert "Systran/faster-whisper-large-v3" in (cr.API.format(repo="Systran/faster-whisper-large-v3"))


def test_upstream_revision_reports_offline_as_empty():
    def broken(url):
        raise OSError("no network")

    assert cr.upstream_revision("Systran/faster-whisper-large-v3", broken) == ""


def test_compare_names_a_changed_revision():
    baseline = {"large-v3": "old", "small": "same"}
    revisions = {"large-v3": "new", "small": "same", "medium": ""}
    assert cr.compare(baseline, revisions) == ["large-v3"]


def test_no_baseline_means_everything_changed():
    assert cr.compare({}, {"large-v3": "new"}) == ["large-v3"]


def test_main_exit_codes(tmp_path, capsys):
    baseline = tmp_path / "model-revisions.json"
    stub = lambda repo: json.dumps({"sha": "r1-" + repo.split("-")[-1]})  # noqa: E731

    original = cr.fetch
    cr.fetch = stub
    try:
        # No baseline yet: the check reports the change and exits 1.
        assert cr.main(["--baseline", str(baseline)]) == 1
        # --update records the baseline and the next run exits 0.
        assert cr.main(["--baseline", str(baseline), "--update"]) == 1
        assert cr.main(["--baseline", str(baseline)]) == 0
    finally:
        cr.fetch = original
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert set(recorded) == set(model_catalog.MODELS)
    assert all(recorded.values()), recorded
    # --update records the baseline and the next run exits 0.
    assert cr.main(["--baseline", str(baseline), "--update"]) == 1
    assert cr.main(["--baseline", str(baseline)]) == 0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert set(recorded) == set(model_catalog.MODELS)
    assert all(recorded.values()), recorded


def test_offline_check_exits_2_and_never_updates_the_baseline(tmp_path, capsys):
    baseline = tmp_path / "model-revisions.json"
    baseline = tmp_path / "model-revisions.json"
    baseline.write_text(json.dumps({"large-v3": "r1"}), encoding="utf-8")

    def broken(url):
        raise OSError("no network")

    original = cr.fetch
    cr.fetch = broken
    try:
        assert cr.main(["--baseline", str(baseline), "--update"]) == 2
    finally:
        cr.fetch = original
    assert json.loads(baseline.read_text(encoding="utf-8")) == {"large-v3": "r1"}
    text = capsys.readouterr().err
    assert "offline" in text


def test_baseline_file_ships_in_tools():
    """The repo carries a baseline so the first release check has a comparison."""
    assert cr.BASELINE.exists()
    data = json.loads(cr.BASELINE.read_text(encoding="utf-8"))
    assert set(data) == set(model_catalog.MODELS)
