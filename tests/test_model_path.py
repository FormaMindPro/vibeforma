"""A model from a folder (F7): a path is never swapped for a repo download.

The configured ``model`` may name a CTranslate2 model folder. ``_resolve_model``
then loads it locally with ``local_files_only=True``, checks ``model.bin`` and
its header plus the files beside it, and refuses a broken folder with
``ModelPathInvalid``. The path is never turned into a Systran repo id and a
folder without ``model.bin`` never falls back to a download.
"""
from __future__ import annotations

import os
import struct
import sys
import types
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first

# A minimal CTranslate2 header: binary version 6, the model name (length
# includes the trailing null), then the model-spec revision.
CT2_NAME = b"WhisperSpec"
CT2_HEADER = (struct.pack("<I", 6)
              + struct.pack("<H", len(CT2_NAME) + 1)
              + CT2_NAME + b"\x00")


class _Log:
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


def _explode(*_args, **_kwargs):
    raise AssertionError("a model folder must never be downloaded")


@pytest.fixture(autouse=True)
def _engine_stubs(monkeypatch):
    """The pure logic needs the engine modules only as patch targets.

    On a machine without the runtime wheels (the Linux CI) the test still
    exercises folder resolution and refusal: the two modules are absent only
    at the real engine call, which every test replaces with a fake.
    """
    if "huggingface_hub" not in sys.modules:
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = _explode
        monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    if "faster_whisper" not in sys.modules:
        engine = types.ModuleType("faster_whisper")

        def _no_engine(*_args, **_kwargs):
            raise AssertionError("the fake engine must replace WhisperModel")

        engine.WhisperModel = _no_engine
        monkeypatch.setitem(sys.modules, "faster_whisper", engine)


def _write_model(folder: Path, *, model_bin: bytes | None = None) -> Path:
    """A model folder with the files a local CT2 Whisper load needs."""
    folder.mkdir(parents=True, exist_ok=True)
    if model_bin is None:
        model_bin = CT2_HEADER + struct.pack("<I", 3) + struct.pack("<I", 0)
    (folder / "model.bin").write_bytes(model_bin)
    (folder / "config.json").write_text(
        '{"alignment_heads": [[0, 0]]}', encoding="utf-8")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
    (folder / "vocabulary.json").write_text("[]", encoding="utf-8")
    return folder


def test_a_model_folder_resolves_to_itself_offline(tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "own-ct2")
    monkeypatch.setattr(asr, "_repo_id", _explode)
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)

    source, offline = asr._resolve_model(str(folder), _Log())

    assert source == os.path.abspath(str(folder))
    assert offline is True


def test_a_folder_is_never_swapped_for_a_systran_repo(tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "own-ct2")
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)

    def repo_id(name):
        raise AssertionError(f"a folder was turned into a repo id: {name}")

    monkeypatch.setattr(asr, "_repo_id", repo_id)

    source, _offline = asr._resolve_model(str(folder), _Log())

    assert source == os.path.abspath(str(folder))


def test_a_folder_without_model_bin_is_refused_with_a_clear_error(
        tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "broken")
    (folder / "model.bin").unlink()
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(folder), _Log())

    message = str(caught.value)
    assert str(folder) in message
    assert "model.bin is missing" in message


def test_a_broken_folder_never_reaches_a_download(tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "broken")
    (folder / "model.bin").unlink()
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)
    monkeypatch.setattr(asr, "_repo_id", _explode)

    with pytest.raises(asr.ModelPathInvalid):
        asr._make_model(str(folder), "cpu", "int8", _Log())


def test_a_missing_folder_path_is_refused_without_a_download(
        tmp_path, monkeypatch):
    missing = tmp_path / "not-there"
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(missing), _Log())

    assert "does not exist" in str(caught.value)


def test_a_file_path_is_refused_as_not_a_folder(tmp_path):
    target = tmp_path / "model.bin"
    target.write_bytes(CT2_HEADER)

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(target), _Log())

    assert "not a folder" in str(caught.value)


@pytest.mark.parametrize(("payload", "expected"), [
    (b"", "model.bin is empty"),
    (b"\x06\x00", "shorter than a CTranslate2 header"),
    (b"<html>not a model</html>", "does not start like a CTranslate2 model"),
    (struct.pack("<I", 6) + struct.pack("<H", 0) + b"XXXXXXXX",
     "does not start like a CTranslate2 model"),
    (CT2_HEADER + b"\x00\x00", "header is truncated"),
])
def test_a_broken_model_bin_is_refused(tmp_path, payload, expected):
    folder = _write_model(tmp_path / "broken", model_bin=payload)

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(folder), _Log())

    assert expected in str(caught.value)


def test_a_folder_without_config_json_is_refused(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")
    (folder / "config.json").unlink()

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(folder), _Log())

    assert "config.json is missing" in str(caught.value)


def test_a_broken_config_json_is_refused(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")
    (folder / "config.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(folder), _Log())

    assert "config.json is not valid JSON" in str(caught.value)


def test_a_folder_without_tokenizer_json_is_refused(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")
    (folder / "tokenizer.json").unlink()

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(folder), _Log())

    assert "tokenizer.json is missing" in str(caught.value)


def test_a_folder_without_vocabulary_is_refused(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")
    (folder / "vocabulary.json").unlink()

    with pytest.raises(asr.ModelPathInvalid) as caught:
        asr._resolve_model(str(folder), _Log())

    assert "vocabulary" in str(caught.value)


def test_the_folder_reaches_the_engine_with_local_files_only(
        tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "own-ct2")
    seen = {}

    class FakeModel:
        def __init__(self, source, **kwargs):
            seen["source"] = source
            seen["kwargs"] = kwargs

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)

    asr._make_model(str(folder), "cpu", "int8", _Log())

    assert seen["source"] == os.path.abspath(str(folder))
    assert seen["kwargs"]["local_files_only"] is True
    assert seen["kwargs"]["device"] == "cpu"


def test_a_failed_folder_load_is_never_retried_online(tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "own-ct2")
    calls = []

    class FailingModel:
        def __init__(self, source, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("broken weights")

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", FailingModel)

    with pytest.raises(RuntimeError):
        asr._make_model(str(folder), "cpu", "int8", _Log())

    assert len(calls) == 1
    assert calls[0]["local_files_only"] is True


def test_check_model_accepts_the_configured_folder_path(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")
    path = str(folder)

    assert asr.check_model("", path) == ""
    assert asr.check_model(path, path) == path
    assert asr.check_model("whisper-1", path) == "whisper-1"


def test_check_model_accepts_the_same_folder_with_another_spelling(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")
    spelled = str(folder) + os.sep

    assert asr.check_model(spelled, str(folder)) == spelled


def test_check_model_refuses_a_catalog_name_against_a_folder(tmp_path):
    folder = _write_model(tmp_path / "own-ct2")

    with pytest.raises(asr.ModelNotInstalled):
        asr.check_model("large-v3-turbo", str(folder))


def test_check_model_never_builds_a_repo_id_from_a_path(tmp_path, monkeypatch):
    folder = _write_model(tmp_path / "own-ct2")

    def repo_id(name):
        raise AssertionError(f"a folder was turned into a repo id: {name}")

    monkeypatch.setattr(asr, "_repo_id", repo_id)

    assert asr.check_model(str(folder), str(folder)) == str(folder)


def test_a_catalog_name_still_resolves_inside_models_dir(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    folder = _write_model(models_dir / "large-v3-turbo")
    monkeypatch.setattr(asr, "MODELS_DIR", str(models_dir))
    monkeypatch.setattr("huggingface_hub.snapshot_download", _explode)

    source, offline = asr._resolve_model("large-v3-turbo", _Log())

    assert source == os.path.abspath(str(folder))
    assert offline is True


def test_a_repo_id_with_a_slash_is_not_a_folder():
    assert asr._model_path("Systran/faster-whisper-large-v3") is None
    assert asr._model_path("dropbox-dash/faster-whisper-large-v3-turbo") is None
