"""The header update control: button, status row and the two actions."""
import pytest

tk = pytest.importorskip("tkinter")


def _root():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for a live Tk widget test")
    root.withdraw()
    return root


class _FakeService:
    def __init__(self, state="idle", tag=""):
        self.update_check_state = state
        self.update_check_tag = tag
        self.calls = []

    def check_updates_now(self):
        self.calls.append("check")
        self.update_check_state = "checking"

    def apply_pending_update(self):
        self.calls.append("apply")
        self.update_check_state = "applying"
        return True

    def dismiss_pending_update(self):
        self.calls.append("dismiss")
        self.update_check_state = "idle"
        self.update_check_tag = ""


def _tr(key, **kw):
    if kw:
        return key + " " + " ".join(str(value) for value in kw.values())
    return key


@pytest.fixture()
def panel():
    import update_control

    root = _root()
    header = tk.Frame(root)
    header.pack(fill="x")
    search = tk.Frame(root)
    search.pack(fill="x")
    svc = _FakeService()
    control = update_control.UpdateControl(
        header, svc=svc, tr=_tr, px=lambda n: n, version="0.4.2",
        bg="#16181D", fg="#D5D9E0", dim="#9AA3B0",
        field_bg="#22262E", accent="#2E86DE", before=search)
    control.refresh()
    root.update_idletasks()
    yield root, svc, control
    root.destroy()


def test_button_starts_idle_in_the_header(panel):
    _root_, _svc, control = panel
    assert control.button.cget("text") == "updates.check"
    assert str(control.button.cget("state")) == "normal"
    assert control.version == "0.4.2"


def test_idle_hides_the_status_row(panel):
    _root_, _svc, control = panel
    assert control.row.winfo_manager() == ""


def test_check_button_asks_the_service(panel):
    _root_, svc, control = panel
    control.button.invoke()
    assert svc.calls == ["check"]
    assert control.status.cget("text") == "updates.checking"
    assert str(control.button.cget("state")) == "disabled"


def test_checking_disables_the_button(panel):
    _root_, svc, control = panel
    svc.update_check_state = "checking"
    control.refresh()
    assert control.row.winfo_manager() == "pack"
    assert control.status.cget("text") == "updates.checking"
    assert str(control.button.cget("state")) == "disabled"
    assert control.button.cget("text") == "updates.checking"
    assert control.download.winfo_manager() == ""


def test_latest_reports_the_running_version(panel):
    _root_, svc, control = panel
    svc.update_check_state = "latest"
    svc.update_check_tag = "0.4.2"
    control.refresh()
    assert control.status.cget("text") == "updates.latest 0.4.2"
    assert str(control.button.cget("state")) == "normal"
    assert control.download.winfo_manager() == ""


def test_found_offers_download_and_later(panel):
    _root_, svc, control = panel
    svc.update_check_state = "found"
    svc.update_check_tag = "v0.4.3"
    control.refresh()
    assert control.status.cget("text") == "updates.available v0.4.3"
    assert control.download.winfo_manager() == "pack"
    assert control.later.winfo_manager() == "pack"


def test_download_and_later_call_the_service(panel):
    _root_, svc, control = panel
    svc.update_check_state = "found"
    svc.update_check_tag = "v0.4.3"
    control.refresh()

    control.download.invoke()

    assert svc.calls == ["apply"]
    assert control.status.cget("text") == "updates.applying v0.4.3"
    assert str(control.button.cget("state")) == "disabled"

    svc.update_check_state = "found"
    svc.update_check_tag = "v0.4.3"
    control.refresh()
    control.later.invoke()

    assert svc.calls == ["apply", "dismiss"]
    assert control.row.winfo_manager() == ""


def test_deferred_keeps_the_offer_visible(panel):
    _root_, svc, control = panel
    svc.update_check_state = "deferred"
    svc.update_check_tag = "v0.4.3"
    control.refresh()
    assert control.status.cget("text") == "updates.deferred"
    assert control.download.winfo_manager() == "pack"
    assert control.later.winfo_manager() == "pack"
    assert str(control.button.cget("state")) == "normal"


def test_source_checkout_explains_and_keeps_the_button(panel):
    _root_, svc, control = panel
    svc.update_check_state = "source_checkout"
    svc.update_check_tag = "v0.4.4"
    control.refresh()
    assert control.status.cget("text") == "updates.source_checkout"
    assert control.row.winfo_manager() == "pack"
    assert control.download.winfo_manager() == ""
    assert control.later.winfo_manager() == ""
    assert str(control.button.cget("state")) == "normal"


def test_applying_disables_the_button(panel):
    _root_, svc, control = panel
    svc.update_check_state = "applying"
    svc.update_check_tag = "v0.4.3"
    control.refresh()
    assert control.status.cget("text") == "updates.applying v0.4.3"
    assert str(control.button.cget("state")) == "disabled"
    assert control.download.winfo_manager() == ""


def test_error_and_install_failed_are_shown(panel):
    _root_, svc, control = panel
    svc.update_check_state = "error"
    control.refresh()
    assert control.status.cget("text") == "updates.failed"
    assert control.row.winfo_manager() == "pack"
    assert control.download.winfo_manager() == ""

    svc.update_check_state = "install_failed"
    svc.update_check_tag = "v0.4.3"
    control.refresh()
    assert control.status.cget("text") == "updates.install_failed v0.4.3"
    assert control.download.winfo_manager() == ""
