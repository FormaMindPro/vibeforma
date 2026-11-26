"""config.json: defaults, merge, corrupt fallback, atomic save, version gate."""
import json
import os
from pathlib import Path

import config
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    return path


def test_load_returns_defaults_when_file_is_missing(config_path):
    cfg = config.load()
    assert cfg["hotkey"] == "ctrl+alt+space"
    assert cfg["language"] == "en"
    assert set(config.DEFAULTS) <= set(cfg)


def test_load_merges_a_partial_file_over_defaults(config_path):
    config_path.write_text(json.dumps({"language": "ru"}), encoding="utf-8")

    cfg = config.load()

    assert cfg["language"] == "ru"
    assert cfg["model"] == config.DEFAULTS["model"]


def test_load_falls_back_to_defaults_on_broken_json(config_path):
    config_path.write_text("{not json", encoding="utf-8")

    assert config.load() == config.DEFAULTS


def test_save_roundtrip_leaves_no_temp_file(config_path):
    config.save({"language": "ru", "model": "small"})

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "language": "ru", "model": "small"}
    assert not os.path.exists(str(config_path) + ".tmp")


def test_failed_replace_keeps_the_previous_config(config_path, monkeypatch):
    config.save({"language": "en"})

    def disk_full(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", disk_full)
    with pytest.raises(OSError):
        config.save({"language": "ru"})

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"language": "en"}


def test_http_api_is_off_by_default():
    assert config.DEFAULTS["http_api"] is False


def _changelog_version():
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## ") and not line.startswith("## Unreleased"):
            return line[3:].split("\u2014")[0].strip()
    raise AssertionError("CHANGELOG.md has no released version heading")


def test_version_matches_the_changelog():
    assert _changelog_version() == config.VERSION


def test_version_is_parseable():
    import updater

    assert updater.version_tuple(config.VERSION) is not None


def test_load_keeps_a_model_folder_path(config_path, tmp_path):
    model_dir = os.path.join(str(tmp_path), "own-ct2-model")
    config_path.write_text(json.dumps({"model": model_dir}), encoding="utf-8")

    cfg = config.load()

    assert cfg["model"] == model_dir


def test_save_roundtrips_a_model_folder_path(config_path, tmp_path):
    model_dir = os.path.join(str(tmp_path), "own-ct2-model")
    config.save({"model": model_dir})

    assert config.load()["model"] == model_dir


def test_load_non_dict_null_falls_back_to_defaults(config_path):
    config_path.write_text("null", encoding="utf-8")

    assert config.load() == config.DEFAULTS


@pytest.mark.parametrize("payload", ["[]", "42", '"a string"'])
def test_load_non_dict_values_fall_back_to_defaults(config_path, payload):
    config_path.write_text(payload, encoding="utf-8")

    assert config.load() == config.DEFAULTS


def test_load_dict_still_merges_after_non_dict_guard(config_path):
    config_path.write_text(json.dumps({"language": "ru"}), encoding="utf-8")

    cfg = config.load()

    assert cfg["language"] == "ru"
    assert cfg["model"] == config.DEFAULTS["model"]
