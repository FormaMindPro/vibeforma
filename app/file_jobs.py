"""File transcription jobs: one queue, one worker, dictation first.

A file job never outranks the person: while a dictation records or is being
recognized the job pauses between segments, and it resumes when the service
is idle again. Cancellation is cooperative for the same reason — the segment
in flight is finished, nothing else is.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import transcript
from transcript import Segment

QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
SKIPPED = "skipped"
TERMINAL = (DONE, ERROR, CANCELLED, SKIPPED)

# The one list of what a folder pass picks up. The single-file tray dialog
# builds its filter from the same tuple, so the two entrances never diverge.
SUPPORTED_MEDIA = (".mp4", ".mkv", ".mov", ".webm", ".mp3",
                   ".wav", ".m4a", ".m4b", ".ogg", ".flac")

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3


class FilePathError(ValueError):
    """The named path is not allowed: outside the roots, a network or device."""


class NoMediaFiles(ValueError):
    """The folder holds no supported audio or video file."""


def _drive_type(path: str) -> int:
    """Windows drive type of a resolved path; 0 on anything else."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return 0
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))
    except (AttributeError, OSError):
        return 0


def _is_local(path: str) -> bool:
    # The product is Windows-only; the Linux CI runs the pure queue tests on
    # temporary folders, where a Windows drive type does not exist.
    if os.name != "nt":
        return os.path.isabs(path)
    return _drive_type(path) in (DRIVE_FIXED, DRIVE_REMOVABLE)


def resolve_source(path: str, roots: list[str] | None = None) -> str:
    """Resolve a path an agent named and refuse what the card forbids.

    An empty ``roots`` list means every local fixed or removable disk; a
    non-empty list means only those trees. Network shares, UNC and device
    paths are refused after ``realpath``, so ``..`` and symlinks cannot walk
    out of a root.
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise FilePathError("empty path")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise FilePathError("network and UNC paths are not allowed")
    if raw.startswith("\\\\.\\") or raw.startswith("\\\\?\\"):
        raise FilePathError("device paths are not allowed")
    resolved = os.path.realpath(raw)
    if not os.path.isfile(resolved):
        raise FilePathError(f"file not found: {resolved}")
    if roots:
        inside = False
        for root in roots:
            base = os.path.realpath(str(root))
            try:
                if os.path.commonpath([resolved, base]) == base:
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            raise FilePathError("path is outside the allowed roots")
    elif not _is_local(resolved):
        raise FilePathError("only local fixed or removable disks are allowed")
    return resolved


def resolve_out(out: str) -> str:
    """Resolve ``--out`` and refuse a non-local destination; "" stays "".

    A UNC, device or network/mapped path would send a transcript off the
    machine, so it is refused before ``realpath``; the locality check applies
    even when ``file_roots`` is set: the roots bound the input, not the exit.
    """
    raw = str(out or "").strip().strip('"')
    if not raw:
        return ""
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise FilePathError("network and UNC paths are not allowed")
    if raw.startswith("\\\\.\\") or raw.startswith("\\\\?\\"):
        raise FilePathError("device paths are not allowed")
    resolved = os.path.realpath(raw)
    if not _is_local(resolved):
        raise FilePathError("only local fixed or removable disks are allowed")
    return resolved


def default_out_path(source: str, fmt: str, out: str = "") -> str:
    """Where one format lands: ``--out`` wins, else the source's own folder.

    ``--out`` naming a folder keeps the source stem inside it; naming a file
    with another extension keeps its stem; nothing is guessed from the disk.
    """
    if out:
        target = os.path.normpath(out)
        if os.path.isdir(target):
            return os.path.join(target,
                                os.path.splitext(os.path.basename(source))[0]
                                + "." + fmt)
        stem, ext = os.path.splitext(target)
        if ext.lower() == "." + fmt:
            return target
        return (stem or target) + "." + fmt
    return os.path.splitext(source)[0] + "." + fmt


def resolve_folder(folder: str, roots: list[str] | None = None,
                   *, trusted: bool = False) -> str:
    """Resolve a folder an agent or the person named and refuse the rest.

    A network, UNC or device path is refused before anything touches the
    disk. ``trusted`` is the tray dialog: the person picked the folder, so
    ``file_roots`` does not apply, but locality still does.
    """
    raw = str(folder or "").strip().strip('"')
    if not raw:
        raise FilePathError("empty path")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise FilePathError("network and UNC paths are not allowed")
    if raw.startswith("\\\\.\\") or raw.startswith("\\\\?\\"):
        raise FilePathError("device paths are not allowed")
    resolved = os.path.realpath(raw)
    if not os.path.isdir(resolved):
        raise FilePathError(f"folder not found: {resolved}")
    if trusted:
        if not _is_local(resolved):
            raise FilePathError("only local fixed or removable disks are allowed")
        return resolved
    if roots:
        inside = False
        for root in roots:
            base = os.path.realpath(str(root))
            try:
                if os.path.commonpath([resolved, base]) == base:
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            raise FilePathError("folder is outside the allowed roots")
    elif not _is_local(resolved):
        raise FilePathError("only local fixed or removable disks are allowed")
    return resolved


def scan_folder(root: str, *, recursive: bool = False) -> list[tuple[str, str]]:
    """Every supported media file under ``root`` as (absolute, relative posix).

    The relative path is sorted by ``casefold``, so the order is deterministic
    and independent of the filesystem. Directories are walked only on
    ``recursive`` and never through a symlink; an unreadable subfolder is
    logged and skipped, never fatal.
    """
    log = logging.getLogger("vibeforma")
    found: list[tuple[str, str]] = []

    def walk(directory: str, prefix: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as e:
            log.warning("Folder scan skipped %s: %s", directory, e)
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        walk(entry.path, prefix + entry.name + "/")
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as e:
                log.warning("Folder scan skipped %s: %s", entry.path, e)
                continue
            if os.path.splitext(entry.name)[1].lower() in SUPPORTED_MEDIA:
                found.append((entry.path, prefix + entry.name))

    walk(root, "")
    found.sort(key=lambda item: item[1].casefold())
    return found


def batch_targets(source: str, formats: list[str], *,
                  out: str = "", root: str = "") -> list[str]:
    """Where a batch file's formats land, one target per requested format.

    Without ``out`` the targets sit beside the source. With ``out`` the
    source's folder inside ``root`` is mirrored under it: a file at the
    top level goes straight into ``out``, ``sub/b.mkv`` becomes
    ``out/sub/b.srt``.
    """
    stem = os.path.splitext(os.path.basename(source))[0]
    if not out:
        return [os.path.join(os.path.dirname(source), stem + "." + str(fmt).lower())
                for fmt in formats]
    try:
        relative = os.path.relpath(source, root or os.path.dirname(source))
    except ValueError as e:
        raise FilePathError(
            f"source is outside the selected folder: {source}") from e
    if os.path.isabs(relative):
        raise FilePathError(
            f"source is outside the selected folder: {source}")
    folder = os.path.dirname(relative)
    targets: list[str] = []
    for fmt in formats:
        parts = [out]
        if folder and folder != ".":
            parts.extend(part for part in folder.split(os.sep) if part)
        parts.append(stem + "." + str(fmt).lower())
        targets.append(os.path.join(*parts))
    for target in targets:
        # Every target must resolve inside ``out``: a relative path that
        # escaped the selected folder is refused, never written.
        if not _inside(target, out):
            raise FilePathError(
                f"source is outside the selected folder: {source}")
    return targets


def _inside(path: str, folder: str) -> bool:
    """Whether ``path`` resolves under ``folder`` (the --out subtree)."""
    try:
        base = os.path.realpath(folder)
        return os.path.commonpath([os.path.realpath(path), base]) == base
    except ValueError:
        return False


def _count_states(children: list[FileJob],
                  fallback_total: int = 0) -> dict[str, int]:
    """The batch counters: total, done, skipped, failed, cancelled, pending."""
    counts = {"total": len(children) or int(fallback_total or 0),
              "done": 0, "skipped": 0, "failed": 0, "cancelled": 0,
              "pending": 0}
    for job in children:
        if job.state == DONE:
            counts["done"] += 1
        elif job.state == SKIPPED:
            counts["skipped"] += 1
        elif job.state == ERROR:
            counts["failed"] += 1
        elif job.state == CANCELLED:
            counts["cancelled"] += 1
        else:
            counts["pending"] += 1
    return counts


@dataclass
class FileJob:
    id: str
    source: str
    language: str = ""
    model: str = ""          # what the caller asked for (status only)
    engine_model: str = ""   # what actually ran; metadata reports this
    formats: list[str] = field(default_factory=list)
    force: bool = False
    out: str = ""
    state: str = QUEUED
    progress: float = 0.0
    segments: list[Segment] = field(default_factory=list)
    error: str | None = None
    duration: float = 0.0
    outputs: list[str] = field(default_factory=list)
    temporary: bool = False
    paused: bool = False
    batch_id: str = ""
    relative: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class FileBatch:
    """One folder pass: ordinary jobs, grouped and summarised once."""

    id: str
    root: str
    recursive: bool = False
    out: str = ""
    language: str = ""
    formats: list[str] = field(default_factory=list)
    force: bool = False
    state: str = QUEUED      # queued | running | paused | done | cancelled
    total: int = 0
    progress: float = 0.0
    current: str = ""        # id of the job in flight, or ""
    children: list[str] = field(default_factory=list)
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    cancelled: bool = False
    # Presentation snapshot for the HUD; recomputed by _refresh_batch.
    current_name: str = ""
    error: str = ""


class FileJobs:
    """The queue behind every file entrance: tray, CLI and HTTP job API."""

    def __init__(self, cfg: dict[str, Any], asr: Any, log: logging.Logger, *,
                 on_event: Any = None, on_batch_event: Any = None,
                 clock: Any = None) -> None:
        self.cfg = cfg
        self.asr = asr
        self.log = log
        self.on_event = on_event
        self.on_batch_event = on_batch_event
        self.clock = clock or time.time
        self._jobs: dict[str, FileJob] = {}
        self._batches: dict[str, FileBatch] = {}
        self._batch_seen: dict[str, tuple[Any, ...]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._dictation = threading.Event()
        self._closed = False
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="vibeforma-file-jobs")
        self._worker.start()

    # -- public ---------------------------------------------------------
    def submit(self, source: str, *, language: str = "", model: str = "",
               formats: list[str] | None = None, force: bool = False,
               out: str = "", temporary: bool = False,
               trusted: bool = False) -> FileJob:
        """Queue one file; the source is validated now, not in the worker.

        ``trusted`` is the tray dialog: the person picked the file, so the
        ``file_roots`` restriction does not apply. Every agent-named path
        goes through ``resolve_source``.
        """
        if trusted:
            resolved = os.path.realpath(str(source or ""))
            if not os.path.isfile(resolved):
                raise FilePathError(f"file not found: {resolved}")
        else:
            resolved = resolve_source(source, list(self.cfg.get("file_roots") or []))
        limit_mb = int(self.cfg.get("file_max_mb") or 0)
        if limit_mb and os.path.getsize(resolved) > limit_mb * 1024 * 1024:
            raise FilePathError(f"file is larger than {limit_mb} MB")
        names = [str(f).lower() for f in (formats or [])]
        for name in names:
            if name not in transcript.FORMATS:
                raise ValueError(f"unknown format: {name}")
        import asr as asr_mod
        requested = asr_mod.check_model(model, str(self.cfg.get("model") or ""))
        job = FileJob(id=uuid.uuid4().hex[:16], source=resolved,
                      language=str(language or ""), model=requested,
                      engine_model=self._engine_model(),
                      formats=names, force=bool(force), out=resolve_out(out),
                      temporary=bool(temporary), created_at=self.clock())
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._purge()
        self._wake.set()
        self.log.info("File job %s queued: %s (%.1f MB)", job.id, resolved,
                      os.path.getsize(resolved) / (1024 * 1024))
        self._emit(job)
        return job

    def submit_folder(self, folder: str, *, language: str = "",
                      model: str = "", formats: list[str] | None = None,
                      out: str = "", recursive: bool = False,
                      force: bool = False, trusted: bool = False) -> FileBatch:
        """Queue every supported file under a folder as one batch.

        The folder, the targets and the existing outputs are settled here,
        before the worker sees anything: an empty folder is refused at once,
        a bad file becomes its own error job, and the rest of the batch goes
        on. No second worker is created — the children are ordinary queue
        entries, so the FIFO and the one-running rule hold unchanged.
        """
        names = [str(f).lower() for f in (formats or ["srt"])]
        for name in names:
            if name not in transcript.FORMATS:
                raise ValueError(f"unknown format: {name}")
        import asr as asr_mod
        requested = asr_mod.check_model(model, str(self.cfg.get("model") or ""))
        resolved = resolve_folder(folder,
                                  list(self.cfg.get("file_roots") or []),
                                  trusted=trusted)
        found = scan_folder(resolved, recursive=bool(recursive))
        out_dir = resolve_out(out)
        if out_dir:
            if os.path.exists(out_dir) and not os.path.isdir(out_dir):
                raise FilePathError(f"not a folder: {out_dir}")
            found = [(source, rel) for source, rel in found
                     if not _inside(source, out_dir)]
        if not found:
            raise NoMediaFiles(f"no supported media files in {resolved}")
        batch = FileBatch(id=uuid.uuid4().hex[:16], root=resolved,
                          recursive=bool(recursive), out=out_dir,
                          language=str(language or ""), formats=names,
                          force=bool(force), total=len(found),
                          created_at=self.clock())
        engine = self._engine_model()
        limit_mb = int(self.cfg.get("file_max_mb") or 0)
        occupied: dict[str, str] = {}
        jobs: list[FileJob] = []
        for source, relative in found:
            job = FileJob(id=uuid.uuid4().hex[:16], source=source,
                          language=batch.language, model=requested,
                          engine_model=engine, force=batch.force,
                          out=out_dir, batch_id=batch.id, relative=relative,
                          created_at=self.clock())
            try:
                checked = self._batch_source(source, trusted)
            except (FilePathError, OSError) as e:
                jobs.append(self._failed(job, str(e)))
                continue
            job.source = checked
            try:
                inside = os.path.commonpath([checked, resolved]) == resolved
            except ValueError:
                inside = False
            if not inside:
                jobs.append(self._failed(
                    job, f"source is outside the selected folder: {checked}"))
                continue
            try:
                targets = batch_targets(checked, names, out=out_dir,
                                        root=resolved)
            except FilePathError as e:
                jobs.append(self._failed(job, str(e)))
                continue
            clash = ""
            for target in targets:
                if os.path.normcase(target) in occupied:
                    clash = target
                    break
            if clash:
                jobs.append(self._failed(
                    job, f"duplicate output target: {clash}"))
                continue
            for target in targets:
                occupied[os.path.normcase(target)] = checked
            missing = [target for target in targets
                       if not os.path.exists(target)]
            if not batch.force and not missing:
                job.state = SKIPPED
                job.progress = 100.0
                job.outputs = list(targets)
                job.finished_at = self.clock()
                jobs.append(job)
                continue
            if not batch.force and missing:
                absent = {os.path.normcase(target) for target in missing}
                job.formats = [fmt for fmt, target in zip(names, targets,
                                                          strict=False)
                               if os.path.normcase(target) in absent]
            else:
                job.formats = list(names)
            try:
                too_big = bool(limit_mb and os.path.getsize(checked)
                               > limit_mb * 1024 * 1024)
            except OSError as e:
                jobs.append(self._failed(job, str(e)))
                continue
            if too_big:
                jobs.append(self._failed(
                    job, f"file is larger than {limit_mb} MB"))
                continue
            if out_dir:
                # Mirror the source folder: the child writes into its own
                # directory, so default_out_path keeps the same stem rule.
                job.out = os.path.dirname(targets[0])
                try:
                    os.makedirs(job.out, exist_ok=True)
                except OSError as e:
                    jobs.append(self._failed(job, str(e)))
                    continue
            jobs.append(job)
        with self._lock:
            self._batches[batch.id] = batch
            for job in jobs:
                self._jobs[job.id] = job
                self._order.append(job.id)
                batch.children.append(job.id)
            self._purge()
        self._wake.set()
        self.log.info("Folder batch %s queued: %s, %d file(s), formats=%s",
                      batch.id, resolved, batch.total, ",".join(names))
        self._refresh_batch(batch)
        return batch

    def get_batch(self, batch_id: str) -> FileBatch | None:
        with self._lock:
            self._purge()
            return self._batches.get(batch_id)

    def active_batches(self) -> list[FileBatch]:
        """Batches not yet done or cancelled, in creation order."""
        with self._lock:
            self._purge()
            return [batch for batch in self._batches.values()
                    if batch.state not in (DONE, CANCELLED)]

    def counts(self, batch: FileBatch) -> dict[str, int]:
        with self._lock:
            children = [self._jobs[cid] for cid in batch.children
                        if cid in self._jobs]
        return _count_states(children, batch.total)

    def get(self, job_id: str) -> FileJob | None:
        with self._lock:
            self._purge()
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        now = self.clock()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in TERMINAL:
                return False
            job.state = CANCELLED
            job.finished_at = now
        self._wake.set()
        self._emit(job)
        self.log.info("File job %s cancelled", job_id)
        return True

    def cancel_batch(self, batch_id: str) -> bool:
        """Stop the current file and the queue of one batch; True when taken.

        The call returns as soon as the cancellation is recorded: the file in
        flight stops between segments. A finished or cancelled batch answers
        False, so a repeated cancel is idempotent.
        """
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None or batch.state in (DONE, CANCELLED):
                return False
            batch.cancelled = True
            children = [self._jobs[cid] for cid in batch.children
                        if cid in self._jobs
                        and self._jobs[cid].state in (QUEUED, RUNNING, PAUSED)]
        for job in children:
            self.cancel(job.id)
        self._wake.set()
        self._refresh_batch(batch)
        self.log.info("Batch %s cancelled", batch_id)
        return True

    def cancel_batches(self) -> int:
        """Cancel every unfinished batch; the number actually cancelled."""
        stopped = 0
        for batch in self.active_batches():
            if self.cancel_batch(batch.id):
                stopped += 1
        return stopped

    def pause(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state not in (QUEUED, RUNNING):
            return False
        job.paused = True
        job.state = PAUSED
        self._emit(job)
        return True

    def resume(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state != PAUSED:
            return False
        job.paused = False
        job.state = QUEUED if job.started_at is None else RUNNING
        self._wake.set()
        self._emit(job)
        return True

    def result(self, job_id: str, fmt: str = "txt") -> str:
        """The named format for a finished job; ValueError while not done."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state != DONE:
            raise ValueError(f"job is {job.state}")
        meta = {"language": job.language or self.cfg.get("language") or "",
                "model": job.model or self.cfg.get("model") or "",
                "duration": job.duration}
        return transcript.render(job.segments, fmt, meta)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._purge()
            jobs = list(self._jobs.values())
        return {
            "jobs_running": sum(1 for j in jobs if j.state in (RUNNING, PAUSED)),
            "jobs_queued": sum(1 for j in jobs if j.state == QUEUED),
        }

    def dictation_active(self, active: bool) -> None:
        """The service's state gate: a dictation makes every job pause."""
        if active:
            self._dictation.set()
        else:
            self._dictation.clear()
        self._wake.set()

    def shutdown(self) -> None:
        self._closed = True
        self._wake.set()
        for job in list(self._jobs.values()):
            if job.state in (QUEUED, RUNNING, PAUSED):
                job.state = CANCELLED
        self._worker.join(timeout=2)

    # -- internals ------------------------------------------------------
    def _emit(self, job: FileJob) -> None:
        if self.on_event is not None:
            with contextlib.suppress(Exception):  # the HUD must never break a job
                self.on_event(job)
        if job.batch_id:
            with contextlib.suppress(Exception):
                batch = self._batches.get(job.batch_id)
                if batch is not None:
                    self._refresh_batch(batch)

    def _failed(self, job: FileJob, message: str) -> FileJob:
        """A batch child that never reaches the worker, with its reason."""
        job.state = ERROR
        job.error = message
        job.finished_at = self.clock()
        return job

    def _batch_source(self, source: str, trusted: bool) -> str:
        """Check one scanned file; the caller records a refusal per file."""
        if trusted:
            resolved = os.path.realpath(source)
            if not os.path.isfile(resolved):
                raise FilePathError(f"file not found: {resolved}")
            if not _is_local(resolved):
                raise FilePathError(
                    "only local fixed or removable disks are allowed")
            return resolved
        return resolve_source(source, list(self.cfg.get("file_roots") or []))

    def _refresh_batch(self, batch: FileBatch) -> None:
        """The single place that recomputes the batch state and its counters.

        Runs after every child event and after a cancel. ``on_batch_event``
        fires only when the visible snapshot changes; the callback runs
        outside the lock and its failure never reaches the worker.
        """
        with self._lock:
            children = [self._jobs[cid] for cid in batch.children
                        if cid in self._jobs]
            counts = _count_states(children, batch.total)
            total = counts["total"]
            complete = (counts["done"] + counts["skipped"]
                        + counts["failed"] + counts["cancelled"])
            current = next((job for job in children
                            if job.state in (RUNNING, PAUSED)), None)
            current_progress = float(current.progress) if current else 0.0
            overall = 0.0
            if total:
                overall = (complete + (current_progress / 100.0 if current else 0.0)) \
                    / total * 100.0
            batch.progress = max(batch.progress, min(100.0, overall))
            batch.current = current.id if current else ""
            failed = [job for job in children if job.state == ERROR]
            batch.error = ""
            if failed:
                last = failed[-1]
                batch.error = f"{last.relative or os.path.basename(last.source)}: " \
                              f"{last.error or ''}"
            if batch.cancelled:
                batch.state = CANCELLED
            elif total == 0 or complete == total:
                batch.state = DONE
            elif current is not None and current.state == PAUSED:
                batch.state = PAUSED
            elif current is not None:
                batch.state = RUNNING
            else:
                batch.state = QUEUED
            if current is not None and batch.started_at is None:
                batch.started_at = current.started_at or self.clock()
            if batch.state == DONE:
                batch.progress = 100.0
            if batch.state in (DONE, CANCELLED):
                if batch.finished_at is None:
                    batch.finished_at = self.clock()
            else:
                batch.finished_at = None
            if current is not None:
                batch.current_name = (current.relative
                                      or os.path.basename(current.source))
            elif batch.state in (DONE, CANCELLED) and children:
                last = children[-1]
                batch.current_name = (last.relative
                                      or os.path.basename(last.source))
            else:
                batch.current_name = ""
            snapshot = (batch.state, counts["total"], counts["done"],
                        counts["skipped"], counts["failed"], counts["cancelled"],
                        counts["pending"], round(batch.progress, 1),
                        batch.current, batch.current_name,
                        round(current_progress, 1), batch.error,
                        batch.finished_at)
            changed = self._batch_seen.get(batch.id) != snapshot
            if changed:
                self._batch_seen[batch.id] = snapshot
        if not changed or self.on_batch_event is None:
            return
        with contextlib.suppress(Exception):  # the HUD must never break a batch
            self.on_batch_event(batch)

    def _purge(self) -> None:
        keep = float(self.cfg.get("job_keep_s") or 0)
        if keep <= 0:
            return
        cutoff = self.clock() - keep
        for batch_id, batch in list(self._batches.items()):
            if batch.state in (DONE, CANCELLED) \
                    and (batch.finished_at or 0) < cutoff:
                for child_id in batch.children:
                    self._jobs.pop(child_id, None)
                self._batches.pop(batch_id, None)
                self._batch_seen.pop(batch_id, None)
        remain: list[str] = []
        for job_id in self._order:
            job = self._jobs.get(job_id)
            if job is None:
                continue
            if job.state in TERMINAL and (job.finished_at or 0) < cutoff:
                batch = self._batches.get(job.batch_id) if job.batch_id else None
                if batch is not None:
                    remain.append(job_id)   # lives and dies with its batch
                    continue
                self._jobs.pop(job_id, None)
                continue
            remain.append(job_id)
        self._order = remain

    def _waiting(self) -> bool:
        """A dictation outranks every file job, including one not started."""
        return self._dictation.is_set()

    def _engine_model(self) -> str:
        """The model actually loaded; a request never changes it."""
        name = str(getattr(self.asr, "model_name", "") or "")
        return name or str(self.cfg.get("model") or "")

    def _next_job(self) -> FileJob | None:
        """Take the next queued job, but never while a dictation is active."""
        if self._waiting():
            return None
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is not None and job.state == QUEUED:
                    return job
        return None

    def _loop(self) -> None:
        while not self._closed:
            job = self._next_job()
            if job is None:
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            try:
                self._run(job)
            except Exception as e:  # keep the worker alive on any failure
                self.log.exception("File job %s crashed: %s", job.id, e)
                if job.state not in TERMINAL:
                    job.state = ERROR
                    job.error = str(e)
                    job.finished_at = self.clock()
                    self._emit(job)

    def _run(self, job: FileJob) -> None:
        with self._lock:
            if job.state in TERMINAL:
                # Cancelled between _next_job and here: no decode, no write.
                return
            job.state = RUNNING
            job.started_at = self.clock()
            job.progress = 0.0
        self._emit(job)
        self.log.info("File job %s started: %s, formats=%s", job.id,
                      job.source, ",".join(job.formats) or "-")
        word_ts = bool(self.cfg.get("file_word_timestamps", False))
        try:
            limit = float(self.cfg.get("file_max_s") or 0.0)
            stream = self.asr.transcribe_file(
                job.source,
                language=job.language or None,
                word_timestamps=word_ts,
                max_s=limit or None,
                on_progress=lambda value: self._progress(job, value),
                on_duration=self._set_duration(job),
                should_pause=lambda: self._pause_check(job),
                should_stop=lambda: job.state == CANCELLED,
            )
            for segment in stream:
                if job.state == CANCELLED:
                    break
                job.segments.append(segment)
            if job.state == CANCELLED:
                job.finished_at = self.clock()
                self._emit(job)
                self.log.info("File job %s cancelled", job.id)
                return
            self._write_outputs(job)
            with self._lock:
                if job.state == CANCELLED:
                    job.finished_at = job.finished_at or self.clock()
                else:
                    job.state = DONE
                    job.progress = 100.0
                    job.finished_at = self.clock()
            self.log.info("File job %s done: %d segments, %.1f s, %d file(s)",
                          job.id, len(job.segments), job.duration,
                          len(job.outputs))
        except Exception as e:  # noqa: BLE001 - a failed job is recorded, never fatal
            job.state = CANCELLED if job.state == CANCELLED else ERROR
            job.error = None if job.state == CANCELLED else str(e)
            job.finished_at = self.clock()
            self.log.warning("File job %s failed: %s", job.id, e)
        finally:
            self._emit(job)
            if job.temporary:
                with contextlib.suppress(OSError):
                    os.remove(job.source)
            self._wake.set()

    def _pause_check(self, job: FileJob) -> bool:
        """Between segments: hold while a dictation runs or the job is paused."""
        blocked = self._dictation.is_set() or job.paused
        if blocked and job.state != PAUSED:
            job.state = PAUSED
            self.log.info("File job %s paused for a dictation", job.id)
            self._emit(job)
        elif not blocked and job.state == PAUSED:
            job.state = RUNNING
            self.log.info("File job %s resumed", job.id)
            self._emit(job)
        return blocked

    def _progress(self, job: FileJob, value: float) -> None:
        job.progress = max(job.progress, min(100.0, float(value)))
        self._emit(job)

    def _set_duration(self, job: FileJob):
        def _apply(value: float) -> None:
            job.duration = max(0.0, float(value))
        return _apply

    def _write_outputs(self, job: FileJob) -> None:
        meta = {"language": job.language or self.cfg.get("language") or "",
                "model": job.model or self.cfg.get("model") or "",
                "duration": job.duration}
        # One atomic phase: a cancel either gets in before it starts (no file
        # is created) or waits for it to end (the full set is written).
        with self._lock:
            if job.state in TERMINAL:
                return
            for fmt in job.formats:
                target = default_out_path(job.source, fmt, job.out)
                if job.batch_id and job.out:
                    base = job.out
                elif job.out:
                    base = (job.out if os.path.isdir(job.out)
                            else (os.path.dirname(job.out) or "."))
                else:
                    base = os.path.dirname(job.source)
                base_path = Path(base).resolve()
                safe_name = Path(target).name
                if not safe_name or safe_name in (".", ".."):
                    raise FilePathError(f"bad output name: {target}")
                destination = (base_path / safe_name).resolve()
                if not destination.is_relative_to(base_path):
                    raise FilePathError(
                        f"refusing to write outside the output folder: {target}")
                if destination.exists() and not job.force:
                    raise FileExistsError(
                        f"{destination} already exists; pass --force to overwrite")
                body = transcript.render(job.segments, fmt, meta)
                base_path.mkdir(parents=True, exist_ok=True)
                with destination.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(body)
                job.outputs.append(str(destination))
