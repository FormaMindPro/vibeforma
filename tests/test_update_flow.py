"""Update flow: a check never installs in notify mode; updating blocks dictation."""
import hashlib
import logging
import os
import shutil
import threading
import types

import config
import main
import pytest
import updater

RELEASE = {
    "tag": "v9.9.9",
    "version": (9, 9, 9),
    "zip": "https://test/z",
}


def service(auto_update):
    cfg = dict(config.DEFAULTS)
    cfg["auto_update"] = auto_update
    log = logging.getLogger("test-update-flow")
    log.addHandler(logging.NullHandler())
    return main.Service(cfg, log)


def test_notify_mode_only_records_the_pending_release(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: dict(RELEASE))
    svc = service("notify")
    svc.state = main.STATE_IDLE
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda *a, **k: applied.append(True))

    svc.check_updates()

    assert svc.pending_update["tag"] == "v9.9.9"
    assert not applied


def test_auto_mode_is_an_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: dict(RELEASE))
    svc = service("auto")
    svc.state = main.STATE_IDLE
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda *a, **k: applied.append(True))

    svc.check_updates()

    assert applied == [True]


def test_hotkey_is_ignored_while_updating(monkeypatch):
    svc = service("notify")
    svc.state = main.STATE_UPDATING

    def forbidden():
        raise AssertionError("recording must not start")

    monkeypatch.setattr(svc, "start_recording", forbidden)
    svc.on_hotkey()


def test_default_update_mode_is_notify():
    assert config.DEFAULTS["auto_update"] == "notify"


class _Tray:
    def __init__(self):
        self.messages = []

    def notify(self, msg, title="VibeForma"):
        self.messages.append(msg)

    def set_state(self, state):
        pass


def test_update_notification_carries_the_changelog(monkeypatch):
    release = dict(RELEASE, body="## Added\n- **Doctor** report",
                   html="https://example.invalid/release")
    monkeypatch.setattr(updater, "latest_release", lambda: release)
    svc = service("notify")
    svc.state = main.STATE_IDLE
    tray = _Tray()
    svc.attach_tray(tray)

    svc.check_updates()

    assert tray.messages
    assert "Doctor" in tray.messages[0]


def test_release_notes_open_only_an_https_page(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    svc = service("notify")
    svc.pending_update = dict(RELEASE, html="http://example.invalid/r")
    assert not svc.open_release_notes()
    assert opened == []

    svc.pending_update = dict(RELEASE, html="https://example.invalid/r")
    assert svc.open_release_notes()
    assert opened == ["https://example.invalid/r"]


def test_manual_check_sets_checking_synchronously(monkeypatch):
    svc = service("notify")
    ran = threading.Event()
    monkeypatch.setattr(svc, "check_updates", lambda *a: ran.set())

    svc.check_updates_now()

    assert svc.update_check_state == "checking"
    assert ran.wait(2.0)


def test_manual_check_reports_the_latest_version(monkeypatch):
    release = dict(RELEASE, tag="v" + config.VERSION,
                   version=updater.version_tuple(config.VERSION))
    monkeypatch.setattr(updater, "latest_release", lambda: release)
    svc = service("notify")

    svc.check_updates(True)

    assert svc.update_check_state == "latest"
    assert svc.update_check_tag == config.VERSION
    assert svc.pending_update is None


def test_found_release_is_offered_and_dismissed(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: dict(RELEASE))
    svc = service("notify")
    svc.state = main.STATE_IDLE

    svc.check_updates(True)

    assert svc.update_check_state == "found"
    assert svc.update_check_tag == "v9.9.9"
    assert svc.pending_update["tag"] == "v9.9.9"

    svc.dismiss_pending_update()

    assert svc.update_check_state == "idle"
    assert svc.update_check_tag == ""
    assert svc.pending_update is None


def test_manual_check_reports_an_error(monkeypatch):
    def broken():
        raise OSError("offline")

    monkeypatch.setattr(updater, "latest_release", broken)
    svc = service("notify")

    svc.check_updates(True)
    assert svc.update_check_state == "error"

    svc.check_updates()
    assert svc.update_check_state == "idle"


def test_apply_while_recording_is_deferred(monkeypatch):
    svc = service("notify")
    svc.pending_update = dict(RELEASE)
    svc.state = main.STATE_RECORDING
    monkeypatch.setattr(svc, "_source_checkout", lambda: False)

    assert svc.apply_pending_update() is False

    assert svc.update_check_state == "deferred"
    assert svc.update_check_tag == "v9.9.9"
    assert svc.pending_update["tag"] == "v9.9.9"
    assert svc._update_busy is False


class _Jobs:
    def __init__(self, batches=(), jobs=()):
        self._batches = list(batches)
        self._jobs = list(jobs)

    def active_batches(self):
        return self._batches

    def active_jobs(self):
        return self._jobs


@pytest.mark.parametrize("batches,jobs", [([object()], []), ([], [object()])])
def test_apply_with_active_file_work_is_deferred(monkeypatch, batches, jobs):
    svc = service("notify")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    svc.file_jobs = _Jobs(batches=batches, jobs=jobs)
    monkeypatch.setattr(svc, "_source_checkout", lambda: False)
    started = []
    monkeypatch.setattr(svc, "_install_via_bootstrap",
                        lambda: started.append(True))

    assert svc.apply_pending_update() is False

    assert svc.update_check_state == "deferred"
    assert svc.pending_update["tag"] == "v9.9.9"
    assert svc._update_busy is False
    assert not started


def _job(state, **extra):
    values = dict(id="job-1", batch_id="", state=state, progress=100.0,
                  segments=[], source="C:/audio.wav", relative="",
                  error="", finished_at=1.0, outputs=[])
    values.update(extra)
    return types.SimpleNamespace(**values)


def test_auto_mode_applies_after_a_file_job_completes(monkeypatch):
    svc = service("auto")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda *a, **k: applied.append(True))

    svc._on_file_event(_job("done"))

    assert applied == [True]


def test_auto_mode_waits_for_a_running_file_job(monkeypatch):
    svc = service("auto")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    svc.file_jobs = _Jobs(jobs=[object()])
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda *a, **k: applied.append(True))

    svc._on_file_event(_job("running"))

    assert not applied


def test_auto_mode_applies_after_a_batch_ends(monkeypatch):
    svc = service("auto")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda *a, **k: applied.append(True))
    batch = types.SimpleNamespace(
        id="batch-1", state="done", total=1, current="", current_name="",
        progress=100.0, cancelled=False, error="", finished_at=1.0)

    svc._on_batch_event(batch)

    assert applied == [True]


def test_source_checkout_is_reported_instead_of_installing(monkeypatch):
    svc = service("notify")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    monkeypatch.setattr(updater, "is_source_checkout", lambda _dir: True)
    started = []
    monkeypatch.setattr(svc, "_install_via_bootstrap",
                        lambda: started.append(True))

    assert svc.apply_pending_update() is False

    assert svc.update_check_state == "source_checkout"
    assert svc.pending_update["tag"] == "v9.9.9"
    assert svc._update_busy is False
    assert not started


def test_prepare_update_verifies_the_downloaded_hash(monkeypatch):
    payload = b"release-bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_download(url, dest, timeout=300):
        if url.endswith(".zip"):
            with open(dest, "wb") as handle:
                handle.write(payload)
        else:
            with open(dest, "w", encoding="ascii") as handle:
                handle.write(digest + "  VibeForma-9.9.9.zip\n")

    monkeypatch.setattr(updater, "download_file", fake_download)
    svc = service("notify")
    rel = dict(RELEASE, zip="https://test/VibeForma-9.9.9.zip",
               sums="https://test/SHA256SUMS")

    zip_path, sums_path = svc._prepare_update(rel)
    folder = os.path.dirname(zip_path)
    try:
        assert os.path.basename(zip_path) == "VibeForma-9.9.9.zip"
        assert os.path.basename(sums_path) == updater.SUMS_NAME
        assert updater.verify_sha256(zip_path, digest)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    assert not os.path.isdir(folder)


def test_prepare_update_rejects_a_mismatch_and_cleans_up(monkeypatch):
    seen = []

    def fake_download(url, dest, timeout=300):
        seen.append(os.path.dirname(dest))
        if url.endswith(".zip"):
            with open(dest, "wb") as handle:
                handle.write(b"release-bytes")
        else:
            with open(dest, "w", encoding="ascii") as handle:
                handle.write("0" * 64 + "  VibeForma-9.9.9.zip\n")

    monkeypatch.setattr(updater, "download_file", fake_download)
    svc = service("notify")
    rel = dict(RELEASE, zip="https://test/VibeForma-9.9.9.zip",
               sums="https://test/SHA256SUMS")

    with pytest.raises(ValueError, match="SHA256 mismatch"):
        svc._prepare_update(rel)

    assert seen
    assert not any(os.path.isdir(folder) for folder in seen)


def test_prepare_update_requires_the_sums_asset(monkeypatch):
    svc = service("notify")
    rel = dict(RELEASE, zip="https://test/VibeForma-9.9.9.zip")

    with pytest.raises(ValueError, match="SHA256SUMS"):
        svc._prepare_update(rel)


def test_failed_install_guard_blocks_auto_retry(monkeypatch):
    svc = service("auto")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    svc._update_failed = True
    started = []
    monkeypatch.setattr(svc, "_install_via_bootstrap",
                        lambda: started.append(True))

    svc._maybe_apply_pending_update()

    assert not started
    assert svc._update_failed is True
    assert svc.pending_update["tag"] == "v9.9.9"


def test_manual_apply_clears_the_guard_and_proceeds(monkeypatch):
    svc = service("notify")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    svc._update_failed = True
    started = []
    monkeypatch.setattr(svc, "_source_checkout", lambda: False)
    monkeypatch.setattr(svc, "_install_via_bootstrap",
                        lambda: started.append(True))

    svc.apply_pending_update()

    assert started == [True]
    assert svc._update_failed is False


def test_check_updates_latest_clears_stale_pending_and_guard(monkeypatch):
    release = dict(RELEASE, tag="v" + config.VERSION,
                   version=updater.version_tuple(config.VERSION))
    monkeypatch.setattr(updater, "latest_release", lambda: release)
    svc = service("auto")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    svc._update_failed = True

    svc.check_updates(True)

    assert svc.pending_update is None
    assert svc._update_failed is False
    assert svc.update_check_state == "latest"
    assert svc.update_check_tag == config.VERSION


def test_skipped_file_job_triggers_auto_retry(monkeypatch):
    svc = service("auto")
    svc.state = main.STATE_IDLE
    svc.pending_update = dict(RELEASE)
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda *a, **k: applied.append(True))

    svc._on_file_event(_job("skipped"))

    assert applied == [True]


def test_failed_relaunch_keeps_the_service_and_sets_the_guard(monkeypatch):
    svc = service("notify")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(svc, "_pythonw_path", lambda: None)
    stopped = []
    monkeypatch.setattr(svc, "shutdown", lambda: stopped.append(True))

    svc._restart_after_update("v9.9.9")

    assert svc._update_failed is True
    assert svc.update_check_state == "install_failed"
    assert not stopped
