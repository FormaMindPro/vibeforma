"""Whisper model revisions: the sidecar marker, the check, the safe update.

A catalog model folder under ``MODELS_DIR`` carries a sidecar marker
``.vibeforma-model.json`` naming the Hugging Face repo and the commit sha the
folder was downloaded at. A background pass compares that sha with the repo's
``main`` revision; a different sha becomes a proposal with an Update button,
never an automatic download. The durable pass state (last check, declined
revisions, baselines for installed folders without a marker) lives beside
``config.json`` in ``DATA_DIR``, never inside the Hugging Face cache, so a
declined revision stays declined after a restart.

The replacement is transactional: the new revision is downloaded into a
temporary folder inside ``MODELS_DIR`` (the same volume, so the final swap is
a rename), validated by ``asr._model_folder_problem``, and swapped in while
the previous folder becomes a backup. The backup is removed only after the
caller has loaded the new model; any failure puts the previous folder back.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import STATE_IDLE, STATE_UPDATING
from .base import ServiceHost

MARKER_NAME = ".vibeforma-model.json"
STATE_NAME = "model_updates.json"

MODEL_UPDATE_IDLE = "idle"
MODEL_UPDATE_CHECKING = "checking"
MODEL_UPDATE_LATEST = "latest"
MODEL_UPDATE_FOUND = "found"
MODEL_UPDATE_UPDATING = "updating"
MODEL_UPDATE_DONE = "done"
MODEL_UPDATE_ERROR = "error"

DEFAULT_INTERVAL_H = 24.0


@dataclass(frozen=True)
class UpdateOffer:
    """One catalog model whose installed revision differs from the repo."""

    model: str
    repo: str
    revision: str


class ModelUpdateError(RuntimeError):
    """The update could not be installed; the previous folder is restored."""


# -- marker ----------------------------------------------------------------
def marker_path(folder: str) -> str:
    """The sidecar marker path inside one model folder."""
    return os.path.join(folder, MARKER_NAME)


def read_marker(folder: str) -> dict[str, str] | None:
    """The marker of a model folder, or None when absent or malformed."""
    try:
        with open(marker_path(folder), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    repo = str(data.get("repo") or "").strip()
    revision = str(data.get("revision") or "").strip()
    if not repo or not revision:
        return None
    return {
        "repo": repo,
        "revision": revision,
        "downloaded_at": str(data.get("downloaded_at") or ""),
    }


def write_marker(folder: str, repo: str, revision: str,
                 downloaded_at: str | None = None) -> None:
    """Write the sidecar atomically: a crash never leaves half a marker."""
    stamp = downloaded_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_json(marker_path(folder), {
        "repo": repo,
        "revision": revision,
        "downloaded_at": stamp,
    })


# -- durable check state ---------------------------------------------------
def state_path(data_dir: str) -> str:
    """Where the durable check state lives: beside config.json, not in HF."""
    return os.path.join(data_dir, STATE_NAME)


def load_state(path: str) -> dict[str, Any]:
    """The durable state; a missing or broken file reads as a first pass."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        last_check = float(data.get("last_check") or 0.0)
    except (TypeError, ValueError):
        last_check = 0.0
    return {
        "last_check": last_check,
        "declined": _string_map(data.get("declined")),
        "baseline": _string_map(data.get("baseline")),
        "notified": _string_map(data.get("notified")),
    }


def save_state(path: str, state: dict[str, Any]) -> None:
    """Write the durable state atomically."""
    _atomic_json(path, state)


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        text = str(item or "").strip()
        if text:
            out[str(key)] = text
    return out


def _atomic_json(path: str, payload: dict[str, Any]) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# -- interval --------------------------------------------------------------
def interval_seconds(cfg: dict[str, Any]) -> float:
    """The configured check interval; 24 h when the key is missing or broken."""
    try:
        hours = float(cfg.get("model_update_interval_h") or DEFAULT_INTERVAL_H)
    except (TypeError, ValueError):
        hours = DEFAULT_INTERVAL_H
    return max(1.0, hours) * 3600.0


def due_for_check(last_check: Any, interval_s: float, now: float) -> bool:
    """Whether another background pass is due."""
    try:
        last = float(last_check or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    return last <= 0.0 or now - last >= max(1.0, interval_s)


# -- the network seam ------------------------------------------------------
class HfRevisionClient:
    """The one network call of the check: the repo's current commit sha.

    ``api_factory`` is the test seam: a stub returns an object whose
    ``model_info`` carries the sha, so the suite never touches the network.
    """

    def __init__(self, api_factory: Callable[[], Any] | None = None) -> None:
        self._api_factory = api_factory

    def revision(self, repo: str) -> str:
        if self._api_factory is None:
            from huggingface_hub import HfApi
            api: Any = HfApi()
        else:
            api = self._api_factory()
        info = api.model_info(repo, revision="main")
        sha = str(getattr(info, "sha", "") or "").strip()
        if not sha:
            raise ValueError(f"Hugging Face returned no commit sha for {repo}")
        return sha


def cached_locally(repo: str) -> bool:
    """Whether the shared HF cache already holds the repo; never online."""
    from huggingface_hub import snapshot_download
    snapshot_download(repo, local_files_only=True)
    return True


def download_revision(repo: str, revision: str, target: str) -> None:
    """Download one revision into a prepared folder (the transactional seam)."""
    from huggingface_hub import snapshot_download
    snapshot_download(repo, revision=revision, local_dir=target)


# -- the check -------------------------------------------------------------
def check_catalog(cfg: dict[str, Any], *, models_dir: str, data_dir: str,
                  client: Any, cache_present: Callable[[str], bool] | None = None,
                  log: Any = None, now: float | None = None) -> list[UpdateOffer]:
    """One pass over the installed catalog models; no download, no deletion.

    A catalog model counts as installed when ``MODELS_DIR/<name>`` holds a
    ``model.bin`` or when the shared HF cache already has the repo. A model
    folder without a marker records its current revision as a baseline on the
    first pass and is never offered by that pass. ``model_folders`` (the
    person's own CT2 folders) are not catalog names and never enter here.
    """
    import asr
    import model_catalog

    stamp = float(now if now is not None else time.time())
    path = state_path(data_dir)
    state = load_state(path)
    present = cache_present or cached_locally
    offers: list[UpdateOffer] = []
    for model in model_catalog.MODELS:
        folder = os.path.join(models_dir, model)
        repo = str(asr._repo_id(model))
        has_folder = os.path.isfile(os.path.join(folder, "model.bin"))
        installed = has_folder
        if not installed:
            try:
                installed = bool(present(repo))
            except Exception as e:  # noqa: BLE001 - an uncached repo is just not installed
                if log is not None:
                    log.debug("Model cache check %s: %s", repo, e)
                installed = False
        if not installed:
            continue
        try:
            remote = str(client.revision(repo))
        except Exception as e:  # noqa: BLE001 - offline leaves the check intact
            if log is not None:
                log.debug("Model revision check %s: %s", repo, e)
            continue
        marker = read_marker(folder) if has_folder else None
        if marker is not None and marker["repo"] != repo:
            # A different repo id means an old marker: re-baseline, offer
            # nothing from a comparison that no longer means anything.
            state["baseline"][model] = remote
            continue
        current = marker["revision"] if marker is not None else state["baseline"].get(model, "")
        if not current:
            state["baseline"][model] = remote
            continue
        if remote == current or state["declined"].get(model) == remote:
            continue
        offers.append(UpdateOffer(model=model, repo=repo, revision=remote))
    state["last_check"] = stamp
    save_state(path, state)
    return offers


# -- the transactional replacement ----------------------------------------
class ModelUpdateTransaction:
    """Download one revision into ``MODELS_DIR`` and swap the folder safely.

    ``download`` is the test seam; when None the module's real
    ``download_revision`` is used, so a stub is one monkeypatch away.
    """

    def __init__(self, model: str, repo: str, revision: str, models_dir: str, *,
                 download: Callable[[str, str, str], None] | None = None,
                 validate: Callable[[str], str | None] | None = None,
                 log: Any = None) -> None:
        self.model = model
        self.repo = repo
        self.revision = revision
        self.models_dir = models_dir
        self.target = os.path.join(models_dir, model)
        self.backup = ""
        self.temp = ""
        self.swapped = False
        self._download = download
        self._validate = validate
        self.log = log

    def download(self) -> None:
        """Fetch the revision into a temporary folder inside MODELS_DIR."""
        os.makedirs(self.models_dir, exist_ok=True)
        infer = tempfile.mkdtemp(prefix=f".update-{self.model}-",
                                 dir=self.models_dir)
        self.temp = infer
        download = self._download or download_revision
        try:
            download(self.repo, self.revision, infer)
        except Exception:
            self._drop(infer)
            self.temp = ""
            raise

    def validate(self) -> None:
        """Refuse a downloaded folder the engine could not load."""
        if not self.temp:
            raise ModelUpdateError("nothing was downloaded")
        validate = self._validate
        if validate is None:
            import asr
            validate = asr._model_folder_problem
        problem = validate(self.temp)
        if problem:
            raise ModelUpdateError(
                f"the downloaded revision is not usable: {problem}")

    def swap(self) -> None:
        """Move the previous folder aside and the new one into place."""
        if not self.temp:
            raise ModelUpdateError("nothing was downloaded")
        if os.path.exists(self.target):
            self.backup = os.path.join(
                self.models_dir, f".backup-{self.model}-{uuid.uuid4().hex}")
            os.replace(self.target, self.backup)
        try:
            os.replace(self.temp, self.target)
        except OSError:
            if self.backup:
                os.replace(self.backup, self.target)
                self.backup = ""
            raise
        self.temp = ""
        self.swapped = True

    def commit(self) -> None:
        """Write the marker for the installed revision and drop the backup."""
        try:
            write_marker(self.target, self.repo, self.revision)
        except OSError as e:
            if self.log is not None:
                self.log.warning("Model marker not written for %s: %s",
                                 self.target, e)
        if self.backup:
            self._drop(self.backup)
            self.backup = ""
        if self.temp:
            self._drop(self.temp)
            self.temp = ""

    def rollback(self) -> None:
        """Remove the new folder and put the previous one back."""
        if self.swapped and os.path.isdir(self.target):
            self._drop(self.target)
        if self.backup and os.path.exists(self.backup):
            os.replace(self.backup, self.target)
            self.backup = ""
        if self.temp:
            self._drop(self.temp)
            self.temp = ""
        self.swapped = False

    def _drop(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)


# -- the service part ------------------------------------------------------
class ModelUpdateActions(ServiceHost):
    """Check, offer and apply model revision updates on the live service."""

    def check_model_updates(self, manual: bool = False) -> None:
        """One revision pass; a background failure never breaks the service."""
        if getattr(self, "_model_check_busy", False):
            return
        self._model_check_busy = True
        previous = self.model_update_state
        if manual:
            self.model_update_state = MODEL_UPDATE_CHECKING
        try:
            import config
            offers = check_catalog(
                self.cfg, models_dir=config.MODELS_DIR,
                data_dir=config.DATA_DIR, client=HfRevisionClient(),
                log=self.log)
            self.pending_model_update = {
                offer.model: offer.revision for offer in offers}
            self.model_update_state = (
                MODEL_UPDATE_FOUND if offers else MODEL_UPDATE_LATEST)
            self._notify_model_revisions(offers)
        except Exception as e:  # noqa: BLE001 - a failed check must not stop the loop
            self.log.debug("Model update check: %s", e)
            self.model_update_state = MODEL_UPDATE_ERROR if manual else previous
        finally:
            self._model_check_busy = False

    def check_model_updates_now(self) -> None:
        """Settings button: a fresh pass with a visible result."""
        if getattr(self, "_model_check_busy", False):
            return
        self.model_update_state = MODEL_UPDATE_CHECKING
        threading.Thread(target=self.check_model_updates, args=(True,),
                         daemon=True).start()

    def decline_pending_model_update(self, model: str = "") -> None:
        """Forget this revision durably: it is not offered again."""
        pending = dict(getattr(self, "pending_model_update", {}) or {})
        name = model or next(iter(pending), "")
        revision = pending.get(name, "")
        if not name or not revision:
            return
        import config
        path = state_path(config.DATA_DIR)
        state = load_state(path)
        state["declined"][name] = revision
        save_state(path, state)
        pending.pop(name, None)
        self.pending_model_update = pending
        if not pending:
            self.model_update_state = MODEL_UPDATE_IDLE
        self.log.info("Model %s revision %s declined", name, revision)

    def apply_pending_model_update(self, model: str = "") -> bool:
        """Consent: start the update when idle; otherwise refuse, write nothing."""
        pending = dict(getattr(self, "pending_model_update", {}) or {})
        name = model or next(iter(pending), "")
        revision = pending.get(name, "")
        if not name or not revision:
            return False
        if (getattr(self, "_model_update_busy", False)
                or self.state != STATE_IDLE or self._model_update_jobs_active()):
            self.log.info("Model update %s refused: the service is %s",
                          name, self.state)
            self._model_update_notify("notify.model_update_busy")
            return False
        import asr
        self._model_update_request = {
            "model": name,
            "repo": str(asr._repo_id(name)),
            "revision": revision,
        }
        self._maybe_apply_pending_model_update()
        return True

    def _maybe_apply_pending_model_update(self) -> None:
        """Start a consented update once the service is idle (idle hook)."""
        request = getattr(self, "_model_update_request", None)
        if not request or getattr(self, "_model_update_busy", False):
            return
        if self.state != STATE_IDLE or self._model_update_jobs_active():
            return
        self._model_update_request = None
        self._model_update_busy = True
        threading.Thread(target=self._model_update_worker,
                         args=(request,), daemon=True).start()

    def _model_update_worker(self, request: dict[str, str]) -> None:
        """Download, validate, swap, load; any failure restores everything."""
        import config
        model = request.get("model", "")
        repo = request.get("repo", "")
        revision = request.get("revision", "")
        old_name = str(self.cfg.get("model") or "")
        active = old_name == model
        tx = ModelUpdateTransaction(model, repo, revision, config.MODELS_DIR,
                                    log=self.log)
        committed = False
        load_attempted = False
        try:
            self._model_update_set_state(STATE_UPDATING)
            self.model_update_state = MODEL_UPDATE_UPDATING
            self._model_update_notify("notify.model_update_started", model=model)
            tx.download()
            tx.validate()
            tx.swap()
            if active and self.asr is not None:
                load_attempted = True
                try:
                    loaded = bool(self.asr.reload_model(model))
                except Exception as e:
                    self.log.exception("Model %s load failed: %s", model, e)
                    loaded = False
                if not loaded:
                    raise ModelUpdateError("the new revision did not load")
            tx.commit()
            committed = True
            self.pending_model_update.pop(model, None)
            self.model_update_state = MODEL_UPDATE_DONE
            self.log.info("Model %s updated to %s", model, revision)
            self._model_update_notify("notify.model_update_ok", model=model)
        except Exception as e:  # noqa: BLE001 - the worker must report, not crash
            self.log.warning("Model %s update failed: %s", model, e)
            if not committed:
                tx.rollback()
            if load_attempted:
                self.cfg["model"] = old_name
                if self.asr is not None:
                    try:
                        self.asr.reload_model(old_name)
                    except Exception as restore_error:  # noqa: BLE001
                        self.log.error("Previous model %s did not reload: %s",
                                       old_name, restore_error)
            self.model_update_state = MODEL_UPDATE_ERROR
            self._model_update_notify("notify.model_update_failed", model=model)
        finally:
            self._model_update_set_state(STATE_IDLE)
            self._model_update_busy = False

    def _notify_model_revisions(self, offers: list[UpdateOffer]) -> None:
        """One tray notification per revision, remembered across restarts."""
        if not offers:
            return
        import config
        path = state_path(config.DATA_DIR)
        state = load_state(path)
        fresh = [offer for offer in offers
                 if state["notified"].get(offer.model) != offer.revision]
        if not fresh:
            return
        for offer in fresh:
            state["notified"][offer.model] = offer.revision
        save_state(path, state)
        names = ", ".join(sorted({offer.model for offer in fresh}))
        self._model_update_notify("notify.model_update_found", model=names)

    def _model_update_jobs_active(self) -> bool:
        """Whether a file job or a folder batch is still unfinished."""
        checker = getattr(self, "_jobs_active", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception as e:  # noqa: BLE001 - update state must never break
            self.log.debug("File job state unavailable: %s", e)
            return False

    def _model_update_set_state(self, state: str) -> None:
        """The service state setter when the host has one (test, embedder)."""
        setter = getattr(self, "_set_state", None)
        if callable(setter):
            setter(state)
        else:
            self.state = state

    def _model_update_notify(self, key: str, **kw: Any) -> None:
        tray = getattr(self, "tray", None)
        if tray is None:
            return
        with contextlib.suppress(Exception):
            tray.notify(self.tr(key, **kw))


# -- the background loop ---------------------------------------------------
def start_model_update_checker(cfg: dict[str, Any], svc: ServiceHost,
                               log: Any) -> None:
    """First due pass shortly after startup, then every configured interval.

    A pass runs only at rest: the check must never compete with a dictation.
    Network failures go to debug and leave the previous offer as it is.
    """
    def _loop() -> None:
        time.sleep(90)
        while True:
            try:
                if cfg.get("model_update_check", True) and svc.state == STATE_IDLE:
                    import config
                    state = load_state(state_path(config.DATA_DIR))
                    if due_for_check(state.get("last_check"),
                                     interval_seconds(cfg), time.time()):
                        svc.check_model_updates()
            except Exception as e:  # noqa: BLE001 - one failed pass must not stop the loop
                log.debug("Model update check: %s", e)
            time.sleep(interval_seconds(cfg))
    threading.Thread(target=_loop, daemon=True).start()
