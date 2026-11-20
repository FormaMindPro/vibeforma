"""Update flow: a check never installs in notify mode; updating blocks dictation."""
import logging

import config
import main
import updater

RELEASE = {
    "tag": "v0.2.0",
    "version": (0, 2, 0),
    "zip": "https://test/z",
    "manifest": "https://test/m",
    "signature": "https://test/s",
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

    assert svc.pending_update["tag"] == "v0.2.0"
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
