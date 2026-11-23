"""Update flow: a check never installs in notify mode; updating blocks dictation."""
import logging
import threading

import config
import main
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
                        lambda: applied.append(True))

    svc.check_updates()

    assert svc.pending_update["tag"] == "v9.9.9"
    assert not applied


def test_auto_mode_is_an_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: dict(RELEASE))
    svc = service("auto")
    svc.state = main.STATE_IDLE
    applied = []
    monkeypatch.setattr(svc, "apply_pending_update",
                        lambda: applied.append(True))

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

    assert svc.apply_pending_update() is False

    assert svc.update_check_state == "deferred"
    assert svc.update_check_tag == "v9.9.9"
    assert svc.pending_update["tag"] == "v9.9.9"
    assert svc._update_busy is False
