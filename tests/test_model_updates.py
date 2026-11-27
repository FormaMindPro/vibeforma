"""Whisper model revision check and the safe update (card-273).

The revision client is a stub and every model folder is a local fixture, so
the suite never touches the network. The durable state (last check, declined
revisions, baselines) lives in a temporary DATA_DIR; the model folders live
in a temporary MODELS_DIR. The update is transactional: a download goes to a
temporary folder inside MODELS_DIR, the previous folder becomes a backup,
and every failure puts it back.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

pytest.importorskip("numpy")

import config
import i18n
from vibeforma import (
    STATE_IDLE,
    STATE_RECORDING,
    model_updates,
)
from vibeforma.model_updates import (
    MODEL_UPDATE_DONE,
    MODEL_UPDATE_ERROR,
    MODEL_UPDATE_IDLE,
    ModelUpdateActions,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

SMALL_REPO = "Systran/faster-whisper-small"

CT2_NAME = b"WhisperSpec"
CT2_HEADER = (struct.pack("<I", 6)
              + struct.pack("<H", len(CT2_NAME) + 1)
              + CT2_NAME + b"\x00")

UPDATE_KEYS = (
    "model.update_g",
    "model.update_found",
    "model.update_now",
    "model.update_later",
    "model.update_check",
    "model.update_checking",
    "model.update_latest",
    "model.update_downloading",
    "model.update_done",
    "model.update_failed",
    "model.update_busy",
    "notify.model_update_found",
    "notify.model_update_started",
    "notify.model_update_ok",
    "notify.model_update_failed",
    "notify.model_update_busy",
)


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


class _Tray:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


class _Engine:
    """Stands in for asr.ASR: sets the shared config like the real engine."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.model = cfg.get("model")
        self.loaded = []
        self.fail_next = False

    def reload_model(self, name):
        self.loaded.append(name)
        self.cfg["model"] = name
        if self.fail_next:
            self.fail_next = False
            self.model = None
            return False
        self.model = name
        return True


class _Host(ModelUpdateActions):
    """The model-update part of the service without the rest of it."""

    def __init__(self, cfg, engine, *, state=STATE_IDLE, jobs=False):
        self.cfg = cfg
        self.asr = engine
        self.log = _Log()
        self.state = state
        self.tray = _Tray()
        self.live = {}
        self.jobs = jobs
        self.model_update_state = MODEL_UPDATE_IDLE
        self.pending_model_update = {}
        self._model_check_busy = False
        self._model_update_busy = False
        self._model_update_request = None

    def _jobs_active(self):
        return self.jobs


class _StubClient:
    """A revision client over a dict; a missing repo reads as offline."""

    def __init__(self, revisions):
        self.revisions = dict(revisions)
        self.asked = []

    def revision(self, repo):
        self.asked.append(repo)
        if repo not in self.revisions:
            raise OSError("offline")
        return self.revisions[repo]


class _Downloads:
    """Writes a valid fixture model into the target folder."""

    def __init__(self, tag=2):
        self.tag = tag
        self.calls = []

    def __call__(self, repo, revision, target):
        self.calls.append((repo, revision, target))
        _write_model(Path(target), tag=self.tag)


def _write_model(folder, *, tag=0):
    """A model folder the loader accepts as a CT2 Whisper model."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "model.bin").write_bytes(
        CT2_HEADER + struct.pack("<I", 3) + struct.pack("<I", tag))
    (folder / "config.json").write_text(
        '{"alignment_heads": [[0, 0]]}', encoding="utf-8")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
    (folder / "vocabulary.json").write_text("[]", encoding="utf-8")
    return folder


def _tag(folder):
    data = (Path(folder) / "model.bin").read_bytes()
    return struct.unpack("<I", data[-4:])[0]


def _catalog(lang):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        return json.load(handle)


def _method(text, name):
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    """Temporary MODELS_DIR and DATA_DIR; the durable state is file-based."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "MODELS_DIR", str(models_dir))
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    return models_dir, data_dir


@pytest.fixture()
def downloads(monkeypatch):
    stub = _Downloads()
    monkeypatch.setattr(model_updates, "download_revision", stub)
    return stub


def _wire(monkeypatch, revisions, *, cache=None):
    """Point the service-level check at the stub client, never the network."""
    client = _StubClient(revisions)
    monkeypatch.setattr(model_updates, "HfRevisionClient", lambda: client)
    present = cache if cache is not None else (lambda _repo: False)
    monkeypatch.setattr(model_updates, "cached_locally", present)
    return client


def _cfg(model="small"):
    return {"model": model, "device": "cpu", "compute_type": "int8",
            "locale": "en"}


# -- the marker ------------------------------------------------------------
def test_the_marker_roundtrips_and_leaves_no_temp_file(tmp_path):
    folder = _write_model(tmp_path / "small")

    model_updates.write_marker(
        str(folder), SMALL_REPO, "sha-1", downloaded_at="2026-11-26T00:00:00Z")

    marker = model_updates.read_marker(str(folder))
    assert marker == {"repo": SMALL_REPO, "revision": "sha-1",
                      "downloaded_at": "2026-11-26T00:00:00Z"}
    assert not list(folder.glob("*.tmp"))
    assert model_updates.read_marker(str(tmp_path / "absent")) is None


def test_a_malformed_marker_reads_as_absent(tmp_path):
    folder = _write_model(tmp_path / "small")
    (folder / model_updates.MARKER_NAME).write_text("{not json", encoding="utf-8")

    assert model_updates.read_marker(str(folder)) is None


# -- the check -------------------------------------------------------------
def test_an_offer_appears_only_for_a_different_commit_sha(dirs, monkeypatch):
    models_dir, data_dir = dirs
    folder = _write_model(models_dir / "small")
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    client = _StubClient({SMALL_REPO: "sha-old"})
    cfg = _cfg()

    same = model_updates.check_catalog(
        cfg, models_dir=str(models_dir), data_dir=str(data_dir),
        client=client, cache_present=lambda _repo: False)
    assert same == []

    client.revisions[SMALL_REPO] = "sha-new"
    changed = model_updates.check_catalog(
        cfg, models_dir=str(models_dir), data_dir=str(data_dir),
        client=client, cache_present=lambda _repo: False)
    assert [(offer.model, offer.repo, offer.revision) for offer in changed] == [
        ("small", SMALL_REPO, "sha-new")]


def test_a_model_without_a_marker_gets_a_baseline_not_an_offer(dirs):
    models_dir, data_dir = dirs
    folder = _write_model(models_dir / "small")
    client = _StubClient({SMALL_REPO: "sha-a"})
    cfg = _cfg()

    first = model_updates.check_catalog(
        cfg, models_dir=str(models_dir), data_dir=str(data_dir),
        client=client, cache_present=lambda _repo: False)

    assert first == []
    assert not (folder / model_updates.MARKER_NAME).exists()
    state = model_updates.load_state(model_updates.state_path(str(data_dir)))
    assert state["baseline"]["small"] == "sha-a"
    assert state["last_check"] > 0

    client.revisions[SMALL_REPO] = "sha-b"
    second = model_updates.check_catalog(
        cfg, models_dir=str(models_dir), data_dir=str(data_dir),
        client=client, cache_present=lambda _repo: False)
    assert [(offer.model, offer.revision) for offer in second] == [
        ("small", "sha-b")]


def test_an_offline_revision_leaves_the_check_intact(dirs):
    models_dir, data_dir = dirs
    _write_model(models_dir / "small")
    client = _StubClient({})   # every repo raises OSError

    offers = model_updates.check_catalog(
        _cfg(), models_dir=str(models_dir), data_dir=str(data_dir),
        client=client, cache_present=lambda _repo: False)

    assert offers == []


def test_local_model_folders_are_never_checked_or_updated(dirs, downloads):
    models_dir, data_dir = dirs
    folder = _write_model(models_dir / "small")
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    own = _write_model(Path(data_dir).parent / "own-ct2", tag=7)
    cfg = _cfg(str(own))
    cfg["model_folders"] = [{"name": "own", "path": str(own)}]
    client = _StubClient({SMALL_REPO: "sha-old"})

    offers = model_updates.check_catalog(
        cfg, models_dir=str(models_dir), data_dir=str(data_dir),
        client=client, cache_present=lambda _repo: False)

    assert offers == []
    assert client.asked == [SMALL_REPO]
    assert not (own / model_updates.MARKER_NAME).exists()
    assert _tag(own) == 7
    host = _Host(cfg, _Engine(cfg))
    assert host.apply_pending_model_update(str(own)) is False
    assert downloads.calls == []


def test_the_interval_defaults_to_24_hours_and_is_respected(dirs):
    assert config.DEFAULTS["model_update_interval_h"] == 24
    assert config.DEFAULTS["model_update_check"] is True
    assert model_updates.interval_seconds({}) == 24 * 3600
    assert model_updates.interval_seconds({"model_update_interval_h": 5}) == 5 * 3600
    now = 1_000_000.0
    assert model_updates.due_for_check(now - 3600, 24 * 3600, now) is False
    assert model_updates.due_for_check(now - 25 * 3600, 24 * 3600, now) is True
    assert model_updates.due_for_check(0, 24 * 3600, now) is True


def test_an_old_config_without_the_keys_reads_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "small"}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))

    cfg = config.load()

    assert cfg["model_update_interval_h"] == 24
    assert cfg["model_update_check"] is True


# -- the service check: offers, decline, notifications ---------------------
def test_a_declined_revision_is_not_offered_after_a_restart(dirs, monkeypatch):
    models_dir, _data_dir = dirs
    folder = _write_model(models_dir / "small")
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    _wire(monkeypatch, {SMALL_REPO: "sha-new"})
    cfg = _cfg()

    first = _Host(cfg, _Engine(cfg))
    first.check_model_updates()
    assert first.pending_model_update == {"small": "sha-new"}
    first.decline_pending_model_update("small")
    assert first.pending_model_update == {}

    # A restart: a fresh host reads the same durable state.
    second = _Host(cfg, _Engine(cfg))
    second.check_model_updates()
    assert second.pending_model_update == {}

    # A newer revision is offered again.
    _wire(monkeypatch, {SMALL_REPO: "sha-newer"})
    third = _Host(cfg, _Engine(cfg))
    third.check_model_updates()
    assert third.pending_model_update == {"small": "sha-newer"}


def test_a_new_revision_notifies_the_tray_once(dirs, monkeypatch):
    models_dir, _data_dir = dirs
    folder = _write_model(models_dir / "small")
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    _wire(monkeypatch, {SMALL_REPO: "sha-new"})
    cfg = _cfg()
    host = _Host(cfg, _Engine(cfg))
    expected = i18n.t(cfg, "notify.model_update_found", model="small")

    host.check_model_updates()
    host.check_model_updates()

    assert host.tray.messages == [expected]


def test_the_check_never_downloads_without_consent(dirs, monkeypatch, downloads):
    models_dir, _data_dir = dirs
    folder = _write_model(models_dir / "small", tag=1)
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    _wire(monkeypatch, {SMALL_REPO: "sha-new"})

    host = _Host(_cfg(), _Engine(_cfg()))
    host.check_model_updates()

    assert host.pending_model_update == {"small": "sha-new"}
    assert downloads.calls == []
    assert _tag(folder) == 1
    assert model_updates.read_marker(str(folder))["revision"] == "sha-old"
    assert not list(models_dir.glob(".update-*"))
    assert not list(models_dir.glob(".backup-*"))


def test_a_busy_service_refuses_with_a_clear_text_and_writes_nothing(
        dirs, monkeypatch, downloads):
    models_dir, _data_dir = dirs
    folder = _write_model(models_dir / "small", tag=1)
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    _wire(monkeypatch, {SMALL_REPO: "sha-new"})
    cfg = _cfg()
    host = _Host(cfg, _Engine(cfg))
    host.check_model_updates()
    host.state = STATE_RECORDING

    assert host.apply_pending_model_update("small") is False

    assert host.pending_model_update == {"small": "sha-new"}
    assert downloads.calls == []
    assert _tag(folder) == 1
    assert model_updates.read_marker(str(folder))["revision"] == "sha-old"
    assert not list(models_dir.glob(".update-*"))
    assert not list(models_dir.glob(".backup-*"))
    assert host.tray.messages[-1] == i18n.t(cfg, "notify.model_update_busy")

    host.state = STATE_IDLE
    host.jobs = True
    assert host.apply_pending_model_update("small") is False
    assert downloads.calls == []


# -- the update worker -----------------------------------------------------
def _offer(dirs, monkeypatch, *, tag=1, revision="sha-new"):
    models_dir, _data_dir = dirs
    folder = _write_model(models_dir / "small", tag=tag)
    model_updates.write_marker(str(folder), SMALL_REPO, "sha-old")
    _wire(monkeypatch, {SMALL_REPO: revision})
    cfg = _cfg()
    host = _Host(cfg, _Engine(cfg))
    host.check_model_updates()
    return host, folder, cfg


def test_a_successful_update_replaces_the_folder_and_writes_the_marker(
        dirs, monkeypatch, downloads):
    host, folder, cfg = _offer(dirs, monkeypatch)
    models_dir, _data_dir = dirs

    host._model_update_worker({"model": "small", "repo": SMALL_REPO,
                               "revision": "sha-new"})

    assert _tag(folder) == 2
    marker = model_updates.read_marker(str(folder))
    assert marker["repo"] == SMALL_REPO
    assert marker["revision"] == "sha-new"
    assert marker["downloaded_at"]
    assert cfg["model"] == "small"
    assert host.asr.loaded == ["small"]
    assert host.asr.model == "small"
    assert not list(models_dir.glob(".update-*"))
    assert not list(models_dir.glob(".backup-*"))
    assert host.model_update_state == MODEL_UPDATE_DONE
    assert host.pending_model_update == {}
    assert host.state == STATE_IDLE


def test_a_successful_update_keeps_the_previous_revision_on_disk(
        dirs, monkeypatch, downloads):
    """The human's decision (card-308): the previous folder is not deleted.

    A kept ``previous-<model>-<revision>`` folder holds the old weights with
    their own marker, so an update that loads fine but behaves worse can be
    rolled back without re-downloading.
    """
    host, folder, cfg = _offer(dirs, monkeypatch)
    models_dir, _data_dir = dirs

    host._model_update_worker({"model": "small", "repo": SMALL_REPO,
                               "revision": "sha-new"})

    kept = list(models_dir.glob("previous-small-*"))
    assert len(kept) == 1, kept
    assert "sha-old" in kept[0].name
    old_marker = model_updates.read_marker(str(kept[0]))
    assert old_marker and old_marker["revision"] == "sha-old"
    assert _tag(kept[0]) == 1
    # The live model carries the new revision; the kept folder's marker is
    # untouched - the rollback is a folder swap, not a re-download.
    assert model_updates.read_marker(str(folder))["revision"] == "sha-new"


def test_a_download_failure_restores_the_folder_and_the_working_model(
        dirs, monkeypatch):
    host, folder, cfg = _offer(dirs, monkeypatch)
    models_dir, _data_dir = dirs

    def _fail(_repo, _revision, _target):
        raise OSError("network down")

    monkeypatch.setattr(model_updates, "download_revision", _fail)

    host._model_update_worker({"model": "small", "repo": SMALL_REPO,
                               "revision": "sha-new"})

    assert _tag(folder) == 1
    assert model_updates.read_marker(str(folder))["revision"] == "sha-old"
    assert cfg["model"] == "small"
    assert host.asr.loaded == []
    assert host.asr.model == "small"
    assert not list(models_dir.glob(".update-*"))
    assert not list(models_dir.glob(".backup-*"))
    assert host.model_update_state == MODEL_UPDATE_ERROR
    assert host.state == STATE_IDLE


def test_a_validation_failure_restores_the_folder(dirs, monkeypatch):
    host, folder, cfg = _offer(dirs, monkeypatch)
    models_dir, _data_dir = dirs

    def _broken(_repo, _revision, target):
        Path(target, "model.bin").write_bytes(b"not a ct2 model")

    monkeypatch.setattr(model_updates, "download_revision", _broken)

    host._model_update_worker({"model": "small", "repo": SMALL_REPO,
                               "revision": "sha-new"})

    assert _tag(folder) == 1
    assert model_updates.read_marker(str(folder))["revision"] == "sha-old"
    assert cfg["model"] == "small"
    assert host.asr.loaded == []
    assert not list(models_dir.glob(".update-*"))
    assert not list(models_dir.glob(".backup-*"))
    assert host.model_update_state == MODEL_UPDATE_ERROR


def test_a_load_failure_restores_the_folder_cfg_and_the_working_model(
        dirs, monkeypatch, downloads):
    host, folder, cfg = _offer(dirs, monkeypatch)
    models_dir, _data_dir = dirs
    host.asr.fail_next = True

    host._model_update_worker({"model": "small", "repo": SMALL_REPO,
                               "revision": "sha-new"})

    assert _tag(folder) == 1
    assert model_updates.read_marker(str(folder))["revision"] == "sha-old"
    assert cfg["model"] == "small"
    # The failed load drops the live model; the rollback loads the old one.
    assert host.asr.loaded == ["small", "small"]
    assert host.asr.model == "small"
    assert not list(models_dir.glob(".update-*"))
    assert not list(models_dir.glob(".backup-*"))
    assert host.model_update_state == MODEL_UPDATE_ERROR
    assert host.state == STATE_IDLE


def test_a_model_only_in_the_hf_cache_gets_a_copy_and_keeps_the_cache(
        dirs, monkeypatch, downloads):
    models_dir, data_dir = dirs
    cache = Path(data_dir).parent / "hf-cache" / "models--small" / "snapshots"
    cached = _write_model(cache / "sha-old", tag=9)
    state = model_updates.load_state(model_updates.state_path(str(data_dir)))
    state["baseline"]["small"] = "sha-old"
    model_updates.save_state(model_updates.state_path(str(data_dir)), state)
    _wire(monkeypatch, {SMALL_REPO: "sha-new"}, cache=lambda _repo: True)
    cfg = _cfg()
    host = _Host(cfg, _Engine(cfg))

    host.check_model_updates()
    assert host.pending_model_update == {"small": "sha-new"}

    host._model_update_worker({"model": "small", "repo": SMALL_REPO,
                               "revision": "sha-new"})

    copy = models_dir / "small"
    assert _tag(copy) == 2
    assert model_updates.read_marker(str(copy))["revision"] == "sha-new"
    assert cached.is_dir() and _tag(cached) == 9
    assert cfg["model"] == "small"
    assert host.asr.model == "small"


def test_the_settings_tab_carries_the_update_controls():
    text = SOURCE.read_text(encoding="utf-8")
    tab = _method(text, "_build_model_tab")
    block = _method(text, "_build_model_update_block")
    assert "_build_model_update_block" in tab
    for name in ("model.update_g", "model.update_now", "model.update_later",
                 "model.update_check", "self.model_update_button",
                 "self.model_check_button", "self._refresh_model_update"):
        assert name in block, name


def test_the_startup_and_service_carry_the_model_update_wiring():
    app_text = (ROOT / "app" / "vibeforma" / "app.py").read_text(encoding="utf-8")
    service_text = (ROOT / "app" / "vibeforma" / "service.py").read_text(
        encoding="utf-8")
    assert "start_model_update_checker(cfg, svc, log)" in app_text
    assert "ModelUpdateActions" in service_text
    assert "_maybe_apply_pending_model_update()" in service_text


def test_every_update_key_is_localized():
    for lang in ("en", "ru"):
        catalog = _catalog(lang)
        missing = [key for key in UPDATE_KEYS if not str(catalog.get(key, "")).strip()]
        assert not missing, (lang, missing)
