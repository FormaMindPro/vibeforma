"""Local model folders on the Model tab (card-264).

A CTranslate2 model folder is a record in ``model_folders``: a name and an
absolute path, nothing else. Validity is computed whenever the list is drawn
(``model.bin`` present), the current model is marked, and switching away from
a folder never deletes its files -- ``model_keep_old`` only removes the copies
VibeForma downloaded under ``MODELS_DIR``. A manual path in ``model`` keeps
working, and an old config without the key reads as an empty list.
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import config
import i18n
import model_folders
import pytest
from vibeforma import STATE_IDLE
from vibeforma import settings_ops as settings_ops_module
from vibeforma.settings_ops import SettingsOps

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

# A minimal CTranslate2 header: binary version 6, the model name (the length
# includes the trailing null), then the model-spec revision.
CT2_NAME = b"WhisperSpec"
CT2_HEADER = (struct.pack("<I", 6)
              + struct.pack("<H", len(CT2_NAME) + 1)
              + CT2_NAME + b"\x00")

KEYS = (
    "model.local_g",
    "model.local_note",
    "model.folder_row",
    "model.folder_valid",
    "model.folder_missing_bin",
    "model.folder_missing_dir",
    "model.folder_invalid",
    "model.folder_current",
    "model.folder_add",
    "model.folder_use",
    "model.folder_remove",
    "model.folder_builtin",
    "model.folder_select",
    "model.folder_added",
    "model.folder_dup",
    "model.folder_limit",
    "model.folder_bad",
    "model.folder_removed",
    "model.folder_current_keep",
    "model.folder_builtin_already",
    "model.custom_desc",
    "dlg.local_note",
)


def _write_model(folder: Path) -> Path:
    """A folder the loader accepts as a local CT2 Whisper model."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "model.bin").write_bytes(
        CT2_HEADER + struct.pack("<I", 3) + struct.pack("<I", 0))
    (folder / "config.json").write_text(
        '{"alignment_heads": [[0, 0]]}', encoding="utf-8")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
    (folder / "vocabulary.json").write_text("[]", encoding="utf-8")
    return folder


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _method(text: str, name: str) -> str:
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


def _catalog(lang: str) -> dict:
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        return json.load(handle)


class _Log:
    """Logger stub: records lines, never writes to disk."""

    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def debug(self, fmt, *args):
        self._add("debug", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)


class _Engine:
    """Stands in for asr.ASR: reload_model sets the shared config like the
    real engine does (the service keeps one cfg dict for both)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.loaded = []

    def reload_model(self, name):
        self.loaded.append(name)
        self.cfg["model"] = name
        return True


class _Host(SettingsOps):
    """The settings part of the service without the rest of it."""

    def __init__(self, cfg: dict, engine: _Engine):
        self.cfg = cfg
        self.asr = engine
        self.log = _Log()
        self.state = STATE_IDLE
        self.tray = None


@pytest.fixture()
def asr_module():
    pytest.importorskip("numpy")
    import asr
    return asr


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    return path


# -- the stored list -------------------------------------------------------
def test_the_list_is_stored_as_a_name_and_an_absolute_path(tmp_path):
    folder = tmp_path / "ct2-large"
    folder.mkdir()

    entries = model_folders.normalize([{"path": str(folder)}])

    assert entries == [{"name": "ct2-large", "path": str(folder)}]
    assert set(entries[0]) == {"name", "path"}
    assert json.loads(json.dumps(entries)) == entries


def test_a_repeated_or_malformed_record_is_dropped(tmp_path):
    folder = tmp_path / "ct2"
    folder.mkdir()

    entries = model_folders.normalize([
        {"path": str(folder)},
        {"path": str(folder) + os.sep},
        {"path": "   "},
        {"name": "no path"},
        "not a record",
        None,
    ])

    assert entries == [{"name": "ct2", "path": str(folder)}]


def test_the_list_is_capped(tmp_path):
    entries = [{"path": str(tmp_path / f"m{index}")}
               for index in range(model_folders.MAX_MODEL_FOLDERS + 5)]

    assert len(model_folders.normalize(entries)) == model_folders.MAX_MODEL_FOLDERS


def test_validity_is_computed_when_the_list_is_drawn(tmp_path):
    folder = _write_model(tmp_path / "ct2")

    assert model_folders.folder_problem(str(folder)) is None

    (folder / "model.bin").unlink()

    assert model_folders.folder_problem(str(folder)) == "missing_model_bin"
    assert model_folders.folder_problem(str(tmp_path / "gone")) == "not_a_folder"


# -- config.json -----------------------------------------------------------
def test_an_old_config_without_the_key_reads_as_an_empty_list(config_path):
    config_path.write_text(json.dumps({"model": "small"}), encoding="utf-8")

    cfg = config.load()

    assert cfg["model_folders"] == []
    assert cfg["model"] == "small"


def test_a_manual_model_path_is_kept_beside_the_folder_list(config_path):
    folder = Path(config_path).parent / "own-ct2"
    config_path.write_text(json.dumps({
        "model": str(folder),
        "model_folders": [{"name": "own", "path": str(folder)}],
    }), encoding="utf-8")

    cfg = config.load()

    assert cfg["model"] == str(folder)
    assert cfg["model_folders"] == [{"name": "own", "path": str(folder)}]


# -- the switch path -------------------------------------------------------
def test_switching_from_a_local_folder_keeps_its_files(
        tmp_path, monkeypatch, asr_module):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    old = _write_model(tmp_path / "old-ct2")
    marker = old / "keep.txt"
    marker.write_text("user file", encoding="utf-8")
    new = _write_model(tmp_path / "new-ct2")
    cfg = {"model": str(old), "device": "cpu", "compute_type": "int8"}
    monkeypatch.setattr(config, "MODELS_DIR", str(models_dir))
    monkeypatch.setattr(settings_ops_module, "save", lambda _cfg: None)
    host = _Host(cfg, _Engine(cfg))
    progress: dict = {}

    host._switch_model_worker(str(new), False, progress)

    assert progress["phase"] == "done"
    assert cfg["model"] == str(new)
    assert marker.read_text(encoding="utf-8") == "user file"
    assert (old / "model.bin").is_file()
    assert progress.get("local_note") == str(old)


def test_a_downloaded_model_inside_models_dir_is_still_removed(
        tmp_path, monkeypatch, asr_module):
    models_dir = tmp_path / "models"
    old = _write_model(models_dir / "small")
    new = _write_model(tmp_path / "new-ct2")
    cfg = {"model": "small", "device": "cpu", "compute_type": "int8"}
    monkeypatch.setattr(config, "MODELS_DIR", str(models_dir))
    monkeypatch.setattr(settings_ops_module, "save", lambda _cfg: None)
    host = _Host(cfg, _Engine(cfg))
    progress: dict = {}

    host._switch_model_worker(str(new), False, progress)

    assert progress["phase"] == "done"
    assert not old.exists()


def test_a_folder_is_never_a_removal_target(tmp_path, asr_module):
    folder = _write_model(tmp_path / "own-ct2")
    models_dir = tmp_path / "models"

    assert model_folders.removal_target(str(folder), str(models_dir)) is None
    assert model_folders.removal_target("small", str(models_dir)) == str(
        models_dir / "small")
    assert model_folders.removal_target("", str(models_dir)) is None


# -- the settings source ---------------------------------------------------
def test_save_writes_the_folder_list():
    save = _method(_source(), "_save")
    assert 'c["model_folders"] = model_folders.normalize(self.model_folders)' in save


def test_the_model_tab_carries_the_folder_controls():
    block = _method(_source(), "_build_model_tab")
    for name in ("model.local_g", "model.local_note", "self.folder_list",
                 "model.folder_add", "model.folder_use",
                 "model.folder_remove", "model.folder_builtin",
                 "self._refresh_model_folders"):
        assert name in block, name


def test_every_folder_key_is_localized():
    for lang in ("en", "ru"):
        catalog = _catalog(lang)
        missing = [key for key in KEYS if not str(catalog.get(key, "")).strip()]
        assert not missing, (lang, missing)


# -- the Model tab on a live Tk root --------------------------------------
@pytest.fixture(scope="module")
def tk_env():
    if sys.platform != "win32":
        pytest.skip("settings_window uses Windows APIs")
    pytest.importorskip("numpy")   # the current mark uses asr._model_path
    tk = pytest.importorskip("tkinter")
    import settings_window
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for a live Tk widget test")
    root.withdraw()
    yield tk, settings_window, root
    root.destroy()


@pytest.fixture()
def settings_module(tk_env):
    return tk_env[1]


@pytest.fixture()
def make_model_tab(tk_env):
    tk, settings_module, root = tk_env

    def build(cfg):
        for child in root.winfo_children():
            child.destroy()

        class _ModelHost(settings_module.SettingsWindow):
            """Only the Model tab, on a bare class shell."""

            def __init__(self):
                self.win = root
                self.cfg = cfg
                self.tr = lambda key, **kw: i18n.t(cfg, key, **kw)
                self._px = lambda value: int(round(value))
                self.rows = {}
                self._search_index = []
                self._frame_key = {}
                self.log = _Log()
                frame = tk.Frame(root)
                self._frame_key[frame] = "model"
                frame.pack()
                self._build_model_tab(frame)

        return _ModelHost()

    yield build
    for child in root.winfo_children():
        child.destroy()


def test_the_tab_lists_name_path_and_validity(make_model_tab, tmp_path):
    folder = _write_model(tmp_path / "ct2-large")
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": [{"path": str(folder)}]})

    row = tab.folder_list.get(0)

    assert "ct2-large" in row
    assert str(folder) in row
    assert tab.tr("model.folder_valid") in row
    assert tab.tr("model.folder_current") not in row


def test_the_current_model_is_marked(make_model_tab, tmp_path):
    folder = _write_model(tmp_path / "current-ct2")
    tab = make_model_tab({"locale": "en", "model": str(folder),
                          "model_folders": [{"path": str(folder)}]})

    assert tab.tr("model.folder_current") in tab.folder_list.get(0)


def test_adding_a_folder_lists_it(make_model_tab, settings_module, tmp_path,
                                  monkeypatch):
    folder = _write_model(tmp_path / "ct2-large")
    monkeypatch.setattr(settings_module.filedialog, "askdirectory",
                        lambda **_kw: str(folder))
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": []})

    tab._add_model_folder()

    assert tab.model_folders == [{"name": "ct2-large", "path": str(folder)}]
    assert tab.folder_list.size() == 1
    assert tab.folder_list.curselection() == (0,)
    assert tab.model_folder_status.cget("text")


def test_a_folder_without_model_bin_is_refused_and_not_added(
        make_model_tab, settings_module, tmp_path, monkeypatch):
    folder = tmp_path / "no-bin"
    folder.mkdir()
    monkeypatch.setattr(settings_module.filedialog, "askdirectory",
                        lambda **_kw: str(folder))
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": []})

    tab._add_model_folder()

    assert tab.model_folders == []
    assert tab.folder_list.size() == 0
    text = tab.model_folder_status.cget("text")
    assert "model.bin" in text
    assert str(folder) in text


def test_a_repeated_folder_is_refused(make_model_tab, settings_module, tmp_path,
                                      monkeypatch):
    folder = _write_model(tmp_path / "ct2-large")
    monkeypatch.setattr(settings_module.filedialog, "askdirectory",
                        lambda **_kw: str(folder))
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": [{"path": str(folder)}]})

    tab._add_model_folder()

    assert len(tab.model_folders) == 1
    assert tab.model_folder_status.cget("text") == tab.tr(
        "model.folder_dup", path=str(folder))


def test_removing_a_record_keeps_the_files(make_model_tab, tmp_path):
    folder = _write_model(tmp_path / "keep-me")
    marker = folder / "notes.txt"
    marker.write_text("mine", encoding="utf-8")
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": [{"path": str(folder)}]})
    tab.folder_list.selection_set(0)

    tab._remove_model_folder()

    assert tab.model_folders == []
    assert marker.read_text(encoding="utf-8") == "mine"
    assert (folder / "model.bin").is_file()


def test_the_current_folder_record_is_kept(make_model_tab, tmp_path):
    folder = _write_model(tmp_path / "current")
    tab = make_model_tab({"locale": "en", "model": str(folder),
                          "model_folders": [{"path": str(folder)}]})
    tab.folder_list.selection_set(0)

    tab._remove_model_folder()

    assert tab.model_folders == [{"name": "current", "path": str(folder)}]
    assert tab.model_folder_status.cget("text") == tab.tr(
        "model.folder_current_keep", name="current")


def test_use_runs_the_existing_switch_flow(make_model_tab, tmp_path):
    folder = _write_model(tmp_path / "own")
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": [{"path": str(folder)}]})
    calls = []
    tab._save = lambda: calls.append(tab.v_model.get())
    tab.folder_list.selection_set(0)

    tab._use_model_folder()

    assert tab.v_model.get() == str(folder)
    assert calls == [str(folder)]


def test_use_refuses_a_folder_without_model_bin_and_keeps_the_model(
        make_model_tab, tmp_path):
    folder = tmp_path / "no-bin"
    folder.mkdir()
    tab = make_model_tab({"locale": "en", "model": "small",
                          "model_folders": [{"path": str(folder)}]})
    calls = []
    tab._save = lambda: calls.append(True)
    tab.folder_list.selection_set(0)

    tab._use_model_folder()

    assert tab.v_model.get() == "small"
    assert calls == []
    assert "model.bin" in tab.model_folder_status.cget("text")


def test_return_to_the_built_in_selects_a_catalog_model(make_model_tab, tmp_path):
    folder = _write_model(tmp_path / "own")
    tab = make_model_tab({"locale": "en", "model": str(folder),
                          "model_folders": [{"path": str(folder)}]})
    calls = []
    tab._save = lambda: calls.append(tab.v_model.get())

    tab._use_builtin_model()

    assert tab.v_model.get() == config.DEFAULT_MODEL
    assert tab.v_model.get() in config.MODELS
    assert calls == [config.DEFAULT_MODEL]


def test_return_to_the_built_in_explains_when_one_is_current(make_model_tab):
    tab = make_model_tab({"locale": "en", "model": config.DEFAULT_MODEL,
                          "model_folders": []})
    calls = []
    tab._save = lambda: calls.append(True)

    tab._use_builtin_model()

    assert calls == []
    assert tab.model_folder_status.cget("text") == tab.tr(
        "model.folder_builtin_already", name=config.DEFAULT_MODEL)


def test_the_done_phase_names_the_kept_folder(settings_module):
    text = settings_module.phase_text(
        lambda key, **kw: i18n.t({"locale": "en"}, key, **kw),
        {"phase": "done", "name": "new-ct2", "local_note": "D:/own-ct2"})

    assert "new-ct2" in text
    assert "not touched" in text


def test_a_folder_switch_is_not_shown_as_a_download(settings_module, tmp_path):
    folder = _write_model(tmp_path / "own-ct2")

    text = settings_module.phase_text(
        lambda key, **kw: i18n.t({"locale": "en"}, key, **kw),
        {"phase": "download", "name": str(folder)})

    assert str(folder) in text
    assert "Downloading" not in text
