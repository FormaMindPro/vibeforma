"""The system recordings folder field on the Recording tab (card-285).

The field shows the effective folder, so an empty key reads as
``system_recording.default_folder()`` and a set key as the configured path.
The live tests build only the Recording tab on a bare Tk root: Change picks a
folder through the dialog, Default shows the default path and Save writes an
empty key for it, Open creates a folder that does not exist yet before it is
handed to the shell, and an unusable folder is reported in ``save_status``
before anything is applied.
"""
import json
import os
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

if sys.platform != "win32":
    pytest.skip("settings_window uses Windows APIs", allow_module_level=True)

import i18n  # noqa: E402
import settings_window  # noqa: E402
import system_recording  # noqa: E402

tk = pytest.importorskip("tkinter")

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

NEW_KEYS = (
    "settings.g_recordings_folder",
    "settings.recording_folder",
    "settings.recording_folder_change",
    "settings.recording_folder_open",
    "settings.recording_folder_default",
    "settings.note_recordings_folder",
    "settings.recording_folder_failed",
    "settings.recording_folder_open_failed",
)


def _source():
    return SOURCE.read_text(encoding="utf-8")


def _method(text, name):
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


def _catalog(lang):
    with open(LOCALES / (lang + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


# -- the group on the Recording tab ---------------------------------------
def test_the_group_lives_under_the_sound_source():
    record = _method(_source(), "_build_record_tab")
    assert "self.v_recordings_folder" in record
    assert "system_recording.folder_for" in record
    assert "settings.note_recordings_folder" in record
    assert (record.index("settings.note_source")
            < record.index("settings.g_recordings_folder"))


def test_every_new_key_is_localized():
    for lang in ("en", "ru"):
        catalog = _catalog(lang)
        missing = [key for key in NEW_KEYS
                   if not str(catalog.get(key, "")).strip()]
        assert not missing, (lang, missing)


# -- the tab and Save on a live Tk root -----------------------------------
class _Service:
    """Records what Save applies; the tab needs no live service."""

    def __init__(self):
        self.applied = []

    def apply_settings(self, cfg):
        self.applied.append(cfg)


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)


class _Host(settings_window.SettingsWindow):
    """Only the Recording tab, built on a bare class shell.

    Save reads the values of every tab; the ones the Recording tab does not
    build are set here as plain variables, so the real ``_save`` runs.
    """

    def __init__(self, root, cfg):
        self.win = root
        self.cfg = cfg
        self.tr = lambda key, **kw: i18n.t(cfg, key, **kw)
        self._px = lambda value: int(round(value))
        self.rows = {}
        self._search_index = []
        self._frame_key = {}
        self.svc = _Service()
        self.log = _Log()
        frame = tk.Frame(root)
        self._frame_key[frame] = "record"
        frame.pack()
        self._build_record_tab(frame)
        self.save_status = tk.Label(root, text="")
        # The other tabs, only as far as _save reads them.
        self.v_keep = tk.BooleanVar(value=False)
        self.v_lang = tk.StringVar(value="English")
        self._lang_by_name = {"English": "en"}
        self.v_ui = tk.StringVar(value="Auto")
        self._ui_by_name = {"Auto": "auto"}
        self.v_paste = tk.StringVar(value="Shift+Insert")
        self.v_restore = tk.BooleanVar(value=True)
        self.v_confirm = tk.BooleanVar(value=False)
        self.v_exclude = tk.BooleanVar(value=True)
        self.v_notify_text = tk.BooleanVar(value=False)
        self.dictionary = []
        self.app_profiles = []
        self.model_folders = []
        self.snippets = []
        self.v_hold = tk.BooleanVar(value=False)
        self.v_history = tk.BooleanVar(value=False)
        self.v_history_app = tk.BooleanVar(value=False)
        self.v_hud = tk.BooleanVar(value=True)
        self.v_alpha = tk.DoubleVar(value=1.0)
        self.v_autogrow = tk.BooleanVar(value=True)
        self.v_hotkey = tk.StringVar(value="ctrl+alt+space")
        self._device_by_name = {}
        self.v_device = tk.StringVar(value="")
        self.v_autostart = tk.BooleanVar(value=False)
        self.v_http = tk.BooleanVar(value=False)
        self.v_hook = tk.StringVar(value="")
        self.v_hook_timeout = tk.IntVar(value=5)
        self.v_upd = tk.BooleanVar(value=True)
        self.v_upd_h = tk.IntVar(value=24)
        self.v_upd_auto = tk.BooleanVar(value=False)
        self.v_model = tk.StringVar(value=cfg["model"])


@pytest.fixture(scope="module")
def tk_root():
    # One Tk root for the module: a fresh Tk() after a destroyed root can hit
    # the "invalid command name tcl_findLibrary" interpreter re-init bug.
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for a live Tk widget test")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture()
def make_host(tk_root):
    def build(cfg=None):
        cfg = dict(cfg or {"locale": "en", "language": "en",
                           "model": "large-v3-turbo"})
        return _Host(tk_root, cfg)

    yield build
    for child in tk_root.winfo_children():
        child.destroy()


def test_the_field_shows_the_default_when_the_key_is_empty(make_host):
    host = make_host()
    assert host.v_recordings_folder.get() == system_recording.folder_for(
        host.cfg)
    assert host.v_recordings_folder.get() == \
        system_recording.default_folder()


def test_the_field_shows_a_configured_folder(make_host, tmp_path):
    folder = tmp_path / "takes"
    host = make_host({"locale": "en", "language": "en",
                      "model": "large-v3-turbo",
                      "system_recordings_dir": str(folder)})
    assert host.v_recordings_folder.get() == os.path.abspath(str(folder))


def test_the_group_carries_change_open_and_default(make_host):
    host = make_host()
    texts = [child.cget("text")
             for child in host.recordings_folder_btns.winfo_children()]
    assert texts == [
        host.tr("settings.recording_folder_change"),
        host.tr("settings.recording_folder_open"),
        host.tr("settings.recording_folder_default"),
    ]


def test_change_picks_the_folder_from_the_dialog(make_host, monkeypatch,
                                                 tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    calls = []

    def askdirectory(**kwargs):
        calls.append(kwargs)
        return str(chosen)

    monkeypatch.setattr(settings_window.filedialog, "askdirectory",
                        askdirectory)
    host = make_host()
    host.v_recordings_folder.set(str(tmp_path / "before"))

    host._change_recordings_folder()

    assert calls, "filedialog.askdirectory was not called"
    assert host.v_recordings_folder.get() == os.path.abspath(str(chosen))


def test_default_shows_the_default_and_save_writes_an_empty_key(
        make_host, monkeypatch, tmp_path):
    documents = tmp_path / "Documents"
    documents.mkdir()
    monkeypatch.setattr(system_recording, "_documents_folder",
                        lambda: str(documents))
    host = make_host()
    default = system_recording.default_folder()

    host.v_recordings_folder.set(str(tmp_path / "other"))
    host._default_recordings_folder()
    assert host.v_recordings_folder.get() == default

    host._save()

    assert host.svc.applied[-1]["system_recordings_dir"] == ""
    assert os.path.isdir(default)


def test_save_writes_the_chosen_folder(make_host, tmp_path):
    host = make_host()
    folder = tmp_path / "takes"

    host.v_recordings_folder.set(str(folder))
    host._save()

    assert host.svc.applied[-1]["system_recordings_dir"] == str(folder)
    assert os.path.isdir(folder)


def test_an_unavailable_folder_is_reported_before_apply(make_host, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    target = blocker / "takes"
    host = make_host()
    host.v_recordings_folder.set(str(target))

    host._save()

    assert host.svc.applied == []
    assert host.save_status.cget("text") == host.tr(
        "settings.recording_folder_failed", path=str(target))
    assert host.v_recordings_folder.get() == str(target)


# -- Open ------------------------------------------------------------------
def test_open_creates_the_folder_before_the_shell(make_host, monkeypatch,
                                                  tmp_path):
    """A folder no take has created yet is made first, then opened."""
    calls = []
    monkeypatch.setattr(settings_window.os, "startfile",
                        lambda p: calls.append(p))
    folder = tmp_path / "takes"
    host = make_host()
    host.v_recordings_folder.set(str(folder))
    assert not folder.exists()

    host._open_recordings_folder()

    assert os.path.isdir(folder)
    assert calls == [os.path.abspath(str(folder))]
    assert host.save_status.cget("text") == ""


def test_open_reports_a_folder_it_cannot_create(make_host, monkeypatch,
                                                tmp_path):
    """A path the writer's rule refuses keeps the localized message."""
    calls = []
    monkeypatch.setattr(settings_window.os, "startfile",
                        lambda p: calls.append(p))
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    target = blocker / "takes"
    host = make_host()
    host.v_recordings_folder.set(str(target))

    host._open_recordings_folder()

    assert calls == []
    assert host.save_status.cget("text") == host.tr(
        "settings.recording_folder_open_failed", path=os.path.abspath(
            str(target)))


def test_open_runs_the_writer_rule_before_startfile():
    """Order in the source: ensure_folder precedes os.startfile."""
    block = _method(_source(), "_open_recordings_folder")
    assert "system_recording.ensure_folder" in block
    assert "os.startfile" in block
    assert (block.index("system_recording.ensure_folder")
            < block.index("os.startfile"))


def test_ensure_folder_is_the_writer_rule_and_public(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder", encoding="utf-8")

    with pytest.raises(system_recording.RecordingsFolderError):
        system_recording.ensure_folder(str(blocker / "takes"))

    created = tmp_path / "created"
    assert system_recording.ensure_folder(str(created)) == str(created)
    assert os.path.isdir(created)
