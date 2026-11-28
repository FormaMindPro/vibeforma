"""Tray requests start the panel on demand when hud is off (F59)."""
import threading

from vibeforma.service import Service


class _Log:
    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


def make_service():
    svc = Service.__new__(Service)
    svc.live = {}
    svc.log = _Log()
    svc.hud_launcher = None
    svc.panel_request = threading.Event()
    return svc


def test_settings_and_doctor_start_the_panel_on_demand():
    svc = make_service()
    started = []
    svc.attach_hud(lambda: started.append("hud"))

    svc.open_settings("history")
    svc.open_doctor()

    assert started == ["hud", "hud"]
    assert svc.live["settings_tab"] == "history"
    assert svc.live["open_settings"] is True
    assert svc.live["open_doctor"] is True


def test_onboarding_starts_the_panel_on_demand():
    svc = make_service()
    started = []
    svc.attach_hud(lambda: started.append("hud"))

    svc.open_onboarding()

    assert started == ["hud"]
    assert svc.live["open_onboarding"] is True


def test_a_panel_request_starts_the_panel_and_shows_it():
    svc = make_service()
    started = []
    svc.attach_hud(lambda: started.append("hud"))

    svc.request_panel()

    assert started == ["hud"]
    assert svc.panel_request.is_set()


def test_a_launcher_failure_does_not_block_the_request():
    svc = make_service()

    def broken():
        raise RuntimeError("no Tk here")

    svc.attach_hud(broken)

    svc.open_settings()

    assert svc.live["open_settings"] is True
