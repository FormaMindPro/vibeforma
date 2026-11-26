"""``model_keep_old=False`` removes only copies inside ``MODELS_DIR`` (card-266).

Switching away from the current model forgets it; it never deletes the
person's own folder. The one deletion the switch performs -- a downloaded
catalog model under ``MODELS_DIR`` -- is confined to that directory: a ``..``
spelling, an absolute, drive or UNC path, a value equal to ``MODELS_DIR``
itself and a directory link pointing outside are never deletion targets. A
user folder with a marker file survives the switch untouched.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import config
import model_folders
import pytest
from vibeforma import STATE_IDLE
from vibeforma import settings_ops as settings_ops_module
from vibeforma.settings_ops import SettingsOps

pytest.importorskip("numpy")

# A minimal CTranslate2 header: binary version 6, the model name (the length
# includes the trailing null), then the model-spec revision.
CT2_NAME = b"WhisperSpec"
CT2_HEADER = (struct.pack("<I", 6)
              + struct.pack("<H", len(CT2_NAME) + 1)
              + CT2_NAME + b"\x00")


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


def _junction(link: Path, target: Path) -> bool:
    """A Windows junction, the link kind that needs no special privilege."""
    try:
        import _winapi
    except ImportError:
        return False
    try:
        _winapi.CreateJunction(str(target), str(link))
        return True
    except OSError:
        return False


def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink or junction; False when not permitted."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return _junction(link, target)


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
    """Stands in for asr.ASR: a successful reload writes the shared config."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def reload_model(self, name):
        self.cfg["model"] = name
        return True


class _Host(SettingsOps):
    """The settings part of the service without the rest of it."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.asr = _Engine(cfg)
        self.log = _Log()
        self.state = STATE_IDLE
        self.tray = None


def _switch(cfg: dict, new_name: str, keep_old: bool, models_dir: Path,
            monkeypatch) -> dict:
    """Run the real switch worker against a temp MODELS_DIR and model folder."""
    monkeypatch.setattr(config, "MODELS_DIR", str(models_dir))
    monkeypatch.setattr(settings_ops_module, "save", lambda _cfg: None)
    progress: dict = {}
    _Host(cfg)._switch_model_worker(new_name, keep_old, progress)
    return progress


# -- the switch itself -----------------------------------------------------
def test_switching_away_from_a_user_folder_keeps_it_and_the_marker(
        tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    user = _write_model(tmp_path / "my-model")
    marker = user / "keep.txt"
    marker.write_text("irreplaceable", encoding="utf-8")
    new = _write_model(tmp_path / "new-model")
    cfg = {"model": str(user)}

    progress = _switch(cfg, str(new), False, models_dir, monkeypatch)

    assert progress["phase"] == "done"
    assert cfg["model"] == str(new)
    assert marker.read_text(encoding="utf-8") == "irreplaceable"
    assert (user / "model.bin").is_file()
    assert user.is_dir()
    assert progress.get("local_note") == str(user)


def test_a_downloaded_model_inside_models_dir_is_still_removed(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    models_dir = tmp_path / "models"
    old = _write_model(models_dir / "small")
    (old / "downloaded.txt").write_text("a copy", encoding="utf-8")
    new = _write_model(tmp_path / "new-model")
    cfg = {"model": "small"}

    progress = _switch(cfg, str(new), False, models_dir, monkeypatch)

    assert progress["phase"] == "done"
    assert not old.exists()
    assert models_dir.is_dir()


def test_a_dotdot_spelling_never_deletes_outside_models_dir(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    models_dir = tmp_path / "models"
    (models_dir / "small").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("not a model", encoding="utf-8")
    new = _write_model(tmp_path / "new-model")
    crafted = "small/../../outside"
    cfg = {"model": crafted}

    progress = _switch(cfg, str(new), False, models_dir, monkeypatch)

    assert progress["phase"] == "done"
    assert marker.read_text(encoding="utf-8") == "not a model"
    assert outside.is_dir()
    assert progress.get("local_note") == crafted


# -- the target guard ------------------------------------------------------
@pytest.mark.parametrize("value", [
    ".",
    "..",
    "foo/..",
    "foo/../..",
    "foo/../../outside",
    "small/../../outside",
    "a /../../outside",
    "small./../../outside",
    "../outside",
])
def test_a_traversal_value_is_never_a_removal_target(tmp_path, value):
    models_dir = tmp_path / "models"
    (models_dir / "foo").mkdir(parents=True)
    (tmp_path / "outside").mkdir()

    assert model_folders.removal_target(value, str(models_dir)) is None


def test_models_dir_itself_is_never_a_target(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    assert model_folders.removal_target(str(models_dir),
                                        str(models_dir)) is None
    monkeypatch.setattr(model_folders, "folder_of", lambda _value: None)

    assert model_folders.removal_target("models/..", str(models_dir)) is None
    assert model_folders.removal_target(".", str(models_dir)) is None


def test_the_guard_holds_even_when_the_value_looks_like_a_name(
        tmp_path, monkeypatch):
    """Containment stands on its own, not only on the folder classifier."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(model_folders, "folder_of", lambda _value: None)

    for value in ("..", "foo/..", "foo/../../outside",
                  "small/../../outside", str(tmp_path / "outside")):
        assert model_folders.removal_target(value, str(models_dir)) is None


@pytest.mark.skipif(sys.platform != "win32",
                    reason="drive letters and UNC names are Windows-only")
def test_a_foreign_drive_or_unc_spelling_is_never_a_target(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    foreign = "Z:" if not str(tmp_path).upper().startswith("Z:") else "Y:"

    for value in (foreign + "\\outside", foreign + ":outside",
                  "\\\\server\\share\\outside"):
        assert model_folders.removal_target(value, str(models_dir)) is None


@pytest.mark.skipif(sys.platform != "win32",
                    reason="drive letters and UNC names are Windows-only")
def test_a_foreign_drive_cannot_become_a_target_even_as_a_name(
        tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    monkeypatch.setattr(model_folders, "folder_of", lambda _value: None)
    foreign = "Z:" if not str(tmp_path).upper().startswith("Z:") else "Y:"

    assert model_folders.removal_target(foreign + "\\outside",
                                        str(models_dir)) is None
    assert model_folders.removal_target(foreign + ":outside",
                                        str(models_dir)) is None


def test_an_absolute_or_outside_path_is_never_a_removal_target(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    user = _write_model(tmp_path / "my-model")
    outside = tmp_path / "outside"
    outside.mkdir()

    for value in (str(user), str(outside)):
        assert model_folders.removal_target(value, str(models_dir)) is None


def test_a_directory_link_pointing_outside_is_never_a_target(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("user data", encoding="utf-8")
    link = models_dir / "linked"
    if not _make_dir_link(link, outside):
        pytest.skip("a directory link cannot be created in this environment")

    assert model_folders.removal_target("linked", str(models_dir)) is None
    assert marker.read_text(encoding="utf-8") == "user data"


def test_a_genuine_downloaded_name_keeps_its_target(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    target = models_dir / "large-v3-turbo"
    target.mkdir()

    assert model_folders.removal_target(
        "large-v3-turbo", str(models_dir)) == str(target)
    assert model_folders.removal_target("", str(models_dir)) is None


# -- reload_model keeps the old model when the switch fails (card-266) -----
# The engine section: a failed switch must not unload the model that dictation
# is serving, and cfg["model"] must never point at a model that is not there.
import asr  # noqa: E402 - the appended section keeps the file's head untouched
import numpy as np  # noqa: E402


class _KeepRow:
    """A faster-whisper segment stand-in."""

    def __init__(self, start=0.0, end=1.0, text="", seek=0):
        self.start = start
        self.end = end
        self.text = text
        self.seek = seek
        self.words = None


class _KeepInfo:
    language = "ru"
    duration = 1.0


class _KeepModel:
    """A loaded-model stand-in: records every call, returns one row."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter([_KeepRow(0.5, 1.0, self.text)]), _KeepInfo()


def _asr_engine(text=" hello"):
    cfg = {"model": "old-model", "device": "cpu", "compute_type": "int8",
           "language": "ru", "hotwords": "", "dictionary": []}
    engine = asr.ASR(cfg, _Log())
    engine.model = _KeepModel(text)
    return engine


def _audio():
    return np.zeros(16000, dtype=np.float32)


def test_a_reload_failure_keeps_the_previous_model_serving(monkeypatch):
    engine = _asr_engine()
    old = engine.model

    def broken(name, device, compute_type, log):
        raise RuntimeError("simulated load failure")

    monkeypatch.setattr(asr, "_make_model", broken)

    assert engine.reload_model("new-model") is False
    assert engine.model is old
    assert engine.cfg["model"] == "old-model"
    assert any(level == "error" for level, _line in engine.log.lines)

    assert engine.transcribe(_audio()) == "hello"
    assert old.calls


def test_a_reload_success_replaces_what_a_failure_keeps(monkeypatch):
    engine = _asr_engine()
    old = engine.model

    def broken(name, device, compute_type, log):
        raise RuntimeError("simulated load failure")

    monkeypatch.setattr(asr, "_make_model", broken)
    assert engine.reload_model("new-model") is False

    loaded = _KeepModel(" replaced")
    monkeypatch.setattr(asr, "_make_model",
                        lambda name, device, compute_type, log: loaded)

    assert engine.reload_model("new-model") is True
    assert engine.model is loaded
    assert engine.cfg["model"] == "new-model"
    assert engine._batched is None
    assert engine._batch_error is None

    assert engine.transcribe(_audio()) == "replaced"
    assert old.calls == []
    assert loaded.calls
