"""Folder mode: scan, batch, outputs, cancel, dictation, CLI, tray, HUD view."""
from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from string import Formatter

import pytest
from transcript import Segment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = os.path.join(ROOT, "app", "locales")


class FakeLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def debug(self, fmt, *args):
        pass

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)


def _segments():
    return [Segment(0.26, 6.7, " first phrase"),
            Segment(6.7, 11.66, " second phrase")]


class _StubASR:
    """Yields segments; a file whose name holds ``bad`` fails like a decoder."""

    model_name = "fake"

    def __init__(self, delay=0.0, rows=None):
        self.delay = delay
        self.rows = rows or _segments()
        self.started = 0

    def transcribe_file(self, path, **kwargs):
        self.started += 1
        if kwargs.get("on_duration"):
            kwargs["on_duration"](12.485)
        if "bad" in os.path.basename(path).lower():
            raise RuntimeError("decoder failed on a broken file")
        for row in self.rows:
            if kwargs.get("should_stop") and kwargs["should_stop"]():
                return
            while kwargs.get("should_pause") and kwargs["should_pause"]():
                time.sleep(0.02)
            if self.delay:
                time.sleep(self.delay)
            if kwargs.get("on_progress"):
                kwargs["on_progress"](min(99.0, row.end / 12.485 * 100))
            yield row
        if kwargs.get("on_progress"):
            kwargs["on_progress"](100.0)


def _jobs(cfg=None, asr=None, on_batch_event=None):
    import file_jobs
    config = {"language": "ru", "model": "fake", "file_max_mb": 4096,
              "job_keep_s": 3600, "file_roots": [],
              "file_word_timestamps": False, "file_max_s": 3600}
    config.update(cfg or {})
    return file_jobs.FileJobs(config, asr or _StubASR(), FakeLog(),
                              on_batch_event=on_batch_event)


def _wait_batch(queue, batch, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if batch.state in ("done", "cancelled"):
            return batch.state
        time.sleep(0.02)
    return batch.state


def _make(folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF")


# -- 1. scan -----------------------------------------------------------
def test_folder_scan_finds_supported_files_in_order_and_skips_others(tmp_path):
    import file_jobs

    _make(tmp_path, ["b.MP3", "a.mp4", "notes.txt", "a.srt", "C.mkv"])
    found = file_jobs.scan_folder(str(tmp_path))
    assert [rel for _abs, rel in found] == ["a.mp4", "b.MP3", "C.mkv"]
    assert all(os.path.isabs(absolute) for absolute, _rel in found)


def test_folder_scan_recursive_and_ignores_directory_links(tmp_path):
    import file_jobs

    _make(tmp_path, ["top.wav", "sub/nested.wav"])
    assert [rel for _abs, rel in file_jobs.scan_folder(str(tmp_path))] == [
        "top.wav"]
    recursive = [rel for _abs, rel in file_jobs.scan_folder(
        str(tmp_path), recursive=True)]
    assert recursive == ["sub/nested.wav", "top.wav"]
    linked = tmp_path / "link"
    try:
        os.symlink(str(tmp_path / "sub"), str(linked),
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        linked = None
    if linked is not None:
        names = [rel for _abs, rel in file_jobs.scan_folder(
            str(tmp_path), recursive=True)]
        assert all(not rel.startswith("link/") for rel in names)


def test_folder_targets_beside_source_and_mirrored_under_out(tmp_path):
    import file_jobs

    folder = tmp_path / "media"
    _make(folder, ["a.mp4", "sub/b.mkv"])
    out = tmp_path / "out"
    beside = file_jobs.batch_targets(str(folder / "a.mp4"), ["srt"],
                                     root=str(folder))
    assert beside == [str(folder / "a.srt")]
    mirrored = file_jobs.batch_targets(str(folder / "sub" / "b.mkv"),
                                       ["srt", "txt"], out=str(out),
                                       root=str(folder))
    assert mirrored == [str(out / "sub" / "b.srt"),
                        str(out / "sub" / "b.txt")]
    nested_out = folder / "out"
    _make(nested_out, ["already.wav"])
    queue = _jobs()
    try:
        batch = queue.submit_folder(str(folder), formats=["srt"],
                                    out=str(nested_out), force=True,
                                    recursive=True)
        assert _wait_batch(queue, batch) == "done"
        relatives = [queue.get(cid).relative for cid in batch.children]
        assert "out/already.wav" not in relatives
        assert (nested_out / "sub" / "b.srt").is_file()
        with pytest.raises(ValueError):
            queue.submit_folder(str(folder), formats=["pdf"])
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(file_jobs.NoMediaFiles):
            queue.submit_folder(str(empty))
    finally:
        queue.shutdown()


# -- 2. outputs --------------------------------------------------------
def test_folder_skips_existing_outputs_and_force_overwrites(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["a.wav"])
    (folder / "a.srt").write_text("old", encoding="utf-8")
    queue = _jobs()
    try:
        batch = queue.submit_folder(str(folder), formats=["srt"])
        assert _wait_batch(queue, batch) == "done"
        job = queue.get(batch.children[0])
        assert job.state == "skipped"
        assert job.progress == 100.0
        assert (folder / "a.srt").read_text(encoding="utf-8") == "old"

        forced = queue.submit_folder(str(folder), formats=["srt"], force=True)
        assert _wait_batch(queue, forced) == "done"
        assert (folder / "a.srt").read_text(encoding="utf-8") != "old"

        (folder / "a.srt").write_text("old", encoding="utf-8")
        if (folder / "a.txt").exists():
            (folder / "a.txt").unlink()
        partial = queue.submit_folder(str(folder), formats=["srt", "txt"])
        assert _wait_batch(queue, partial) == "done"
        pjob = queue.get(partial.children[0])
        assert pjob.formats == ["txt"]
        assert (folder / "a.srt").read_text(encoding="utf-8") == "old"
        assert (folder / "a.txt").is_file()
    finally:
        queue.shutdown()


def test_folder_duplicate_stems_fail_the_second_file_not_the_batch(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["a.mp4", "a.mkv", "b.wav"])
    queue = _jobs()
    try:
        for force in (False, True):
            batch = queue.submit_folder(str(folder), formats=["srt"],
                                        force=force)
            assert _wait_batch(queue, batch) == "done"
            children = [queue.get(cid) for cid in batch.children]
            errors = [job for job in children if job.state == "error"]
            assert len(errors) == 1
            assert "duplicate output target" in (errors[0].error or "")
            assert sum(1 for job in children if job.state == "done") == 2
    finally:
        queue.shutdown()


def test_folder_one_bad_file_does_not_stop_the_batch(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["good1.wav", "bad.wav", "good2.wav"])
    queue = _jobs()
    try:
        batch = queue.submit_folder(str(folder), formats=["srt"])
        assert _wait_batch(queue, batch) == "done"
        states = {queue.get(cid).relative: queue.get(cid).state
                  for cid in batch.children}
        assert states == {"good1.wav": "done", "bad.wav": "error",
                          "good2.wav": "done"}
        counts = queue.counts(batch)
        assert counts["failed"] == 1 and counts["done"] == 2
        assert batch.state == "done"
        assert (folder / "good1.srt").is_file()
        assert (folder / "good2.srt").is_file()
        assert not (folder / "bad.srt").exists()
    finally:
        queue.shutdown()


def test_folder_batch_progress_is_overall_and_per_file(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["good1.wav", "good2.wav", "ready.wav"])
    (folder / "ready.srt").write_text("ready", encoding="utf-8")
    queue = _jobs(asr=_StubASR(delay=0.04))
    try:
        batch = queue.submit_folder(str(folder), formats=["srt"])
        # The skipped file is complete at once: 1 of 3.
        assert batch.progress == pytest.approx(100.0 / 3, abs=0.1)
        samples = [batch.progress]
        deadline = time.time() + 15
        while time.time() < deadline and batch.state != "done":
            samples.append(batch.progress)
            time.sleep(0.01)
        assert batch.state == "done"
        assert samples == sorted(samples)
        assert batch.progress == 100.0
        for cid in batch.children:
            assert 0.0 <= queue.get(cid).progress <= 100.0
    finally:
        queue.shutdown()


# -- 3. cancel, dictation, locality -----------------------------------
def test_folder_batch_cancel_stops_the_current_and_the_queue(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["one.wav", "two.wav", "three.wav"])
    queue = _jobs(asr=_StubASR(delay=0.08))
    try:
        batch = queue.submit_folder(str(folder), formats=["srt"])
        deadline = time.time() + 10
        while time.time() < deadline and not batch.current:
            time.sleep(0.02)
        assert batch.current
        assert queue.cancel_batch(batch.id) is True
        assert queue.cancel_batch(batch.id) is False
        assert _wait_batch(queue, batch) == "cancelled"
        assert batch.cancelled is True
        for cid in batch.children:
            assert queue.get(cid).state == "cancelled"
        assert not (folder / "one.srt").exists()
    finally:
        queue.shutdown()


def test_folder_batch_pauses_for_dictation_and_resumes(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["one.wav", "two.wav"])
    asr = _StubASR(delay=0.04)
    queue = _jobs(asr=asr)
    try:
        queue.dictation_active(True)
        batch = queue.submit_folder(str(folder), formats=["srt"])
        time.sleep(0.2)
        assert asr.started == 0
        assert batch.state == "queued"
        queue.dictation_active(False)
        deadline = time.time() + 10
        while time.time() < deadline and batch.state != "running":
            time.sleep(0.02)
        assert batch.state == "running"
        queue.dictation_active(True)
        deadline = time.time() + 10
        while time.time() < deadline and batch.state != "paused":
            time.sleep(0.02)
        assert batch.state == "paused"
        paused_job = queue.get(batch.current)
        assert paused_job.state == "paused"
        queue.dictation_active(False)
        assert _wait_batch(queue, batch) == "done"
        assert paused_job.state == "done"
    finally:
        queue.shutdown()


def test_folder_batch_uses_no_network_socket(tmp_path, monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the folder batch must not open a network socket")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    folder = tmp_path / "media"
    _make(folder, ["one.wav", "two.wav"])
    queue = _jobs()
    try:
        batch = queue.submit_folder(str(folder), formats=["srt"])
        assert _wait_batch(queue, batch) == "done"
        assert (folder / "one.srt").is_file()
        assert (folder / "two.srt").is_file()
    finally:
        queue.shutdown()


def test_folder_outside_roots_and_unc_refused(tmp_path):
    import file_jobs

    allowed = tmp_path / "allowed"
    _make(allowed, ["a.wav"])
    outside = tmp_path / "outside"
    _make(outside, ["b.wav"])
    with pytest.raises(file_jobs.FilePathError):
        file_jobs.resolve_folder(str(outside), [str(allowed)])
    with pytest.raises(file_jobs.FilePathError):
        file_jobs.resolve_folder("\\\\server\\share\\media")
    with pytest.raises(file_jobs.FilePathError):
        file_jobs.resolve_folder("\\\\server\\share\\media", trusted=True)
    assert file_jobs.resolve_folder(str(outside), [str(allowed)],
                                    trusted=True) == os.path.realpath(outside)
    assert file_jobs.resolve_folder(str(allowed), [str(allowed)]) \
        == os.path.realpath(allowed)


# -- 3b. cancel races and path guards ---------------------------------
def test_folder_batch_cancel_before_start_writes_nothing(tmp_path):
    folder = tmp_path / "media"
    _make(folder, ["one.wav"])
    queue = _jobs()
    try:
        # A dictation keeps the worker away, so the whole batch is cancelled
        # while every child is still queued: no decode, no output.
        queue.dictation_active(True)
        batch = queue.submit_folder(str(folder), formats=["srt"])
        assert queue.cancel_batch(batch.id) is True
        queue.dictation_active(False)
        assert _wait_batch(queue, batch) == "cancelled"
        job = queue.get(batch.children[0])
        assert job.state == "cancelled"
        assert job.outputs == []
        assert not (folder / "one.srt").exists()
    finally:
        queue.shutdown()


def test_folder_batch_cancel_during_write_leaves_no_partial_outputs(
        tmp_path, monkeypatch):
    import file_jobs

    folder = tmp_path / "media"
    _make(folder, ["one.wav"])
    queue = _jobs()
    started = threading.Event()
    release = threading.Event()
    original = file_jobs.transcript.render

    def _slow_render(*args, **kwargs):
        started.set()
        assert release.wait(5.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(file_jobs.transcript, "render", _slow_render)
    answers: list[bool] = []
    try:
        batch = queue.submit_folder(str(folder), formats=["srt", "txt"])
        assert started.wait(10.0)
        canceller = threading.Thread(
            target=lambda: answers.append(queue.cancel_batch(batch.id)))
        canceller.start()
        time.sleep(0.1)
        release.set()
        canceller.join(10.0)
        assert not canceller.is_alive()
        assert _wait_batch(queue, batch) == batch.state
        job = queue.get(batch.children[0])
        targets = {os.path.join(str(folder), "one.srt"),
                   os.path.join(str(folder), "one.txt")}
        # The write phase was already holding the lock: the cancel either
        # waited for the full set or found the job done. A partial set is
        # never acceptable.
        assert set(job.outputs) == targets
        assert all(os.path.isfile(path) for path in targets)
        if answers and answers[0]:
            assert job.state == "cancelled"
        else:
            assert job.state == "done"
    finally:
        queue.shutdown()


def test_folder_scan_skips_file_symlinks(tmp_path):
    import file_jobs

    _make(tmp_path, ["real.wav"])
    link = tmp_path / "linked.wav"
    try:
        os.symlink(str(tmp_path / "real.wav"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are not available")
    found = file_jobs.scan_folder(str(tmp_path))
    assert [rel for _abs, rel in found] == ["real.wav"]


def test_batch_targets_never_emit_parent_segments_or_crash_across_drives(
        tmp_path, monkeypatch):
    import file_jobs

    root = tmp_path / "root"
    root.mkdir()
    source = root / "sub" / "a.wav"
    source.parent.mkdir()
    source.write_bytes(b"RIFF")
    out = tmp_path / "out"
    targets = file_jobs.batch_targets(str(source), ["srt"], out=str(out),
                                      root=str(root))
    assert targets == [str(out / "sub" / "a.srt")]
    assert all(".." not in os.path.normpath(path).split(os.sep)
               for path in targets)
    outside = tmp_path / "outside" / "b.wav"
    outside.parent.mkdir()
    outside.write_bytes(b"RIFF")
    with pytest.raises(file_jobs.FilePathError):
        file_jobs.batch_targets(str(outside), ["srt"], out=str(out),
                                root=str(root))

    def _cross_drive(*_args, **_kwargs):
        raise ValueError("path is on a different drive")

    monkeypatch.setattr(file_jobs.os.path, "relpath", _cross_drive)
    with pytest.raises(file_jobs.FilePathError):
        file_jobs.batch_targets(str(source), ["srt"], out=str(out),
                                root=str(root))


def test_folder_file_outside_selected_root_is_per_file_error(
        tmp_path, monkeypatch):
    import file_jobs

    root = tmp_path / "root"
    _make(root, ["inside.wav"])
    outside = tmp_path / "outside"
    _make(outside, ["outside.wav"])

    def _fake_scan(_root, *, recursive=False):
        return [(str(outside / "outside.wav"), "outside.wav")]

    monkeypatch.setattr(file_jobs, "scan_folder", _fake_scan)
    queue = _jobs()
    try:
        batch = queue.submit_folder(str(root), formats=["srt"])
        assert _wait_batch(queue, batch) == "done"
        job = queue.get(batch.children[0])
        assert job.state == "error"
        assert "source is outside the selected folder" in (job.error or "")
    finally:
        queue.shutdown()


def test_out_locality_refused_for_single_and_folder(tmp_path):
    import file_jobs

    folder = tmp_path / "media"
    _make(folder, ["a.wav"])
    queue = _jobs()
    try:
        assert file_jobs.resolve_out("") == ""
        for bad in ("\\\\server\\share\\out", "\\\\.\\pipe\\out"):
            with pytest.raises(file_jobs.FilePathError):
                file_jobs.resolve_out(bad)
            with pytest.raises(file_jobs.FilePathError):
                queue.submit(str(folder / "a.wav"), formats=["srt"], out=bad)
            with pytest.raises(file_jobs.FilePathError):
                queue.submit_folder(str(folder), formats=["srt"], out=bad)
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(file_jobs.FilePathError):
            queue.submit_folder(str(folder), formats=["srt"],
                                out=str(blocker))
    finally:
        queue.shutdown()


def test_pipe_out_refusal_answers_code_two():
    import file_jobs
    from vibeforma.service import Service

    class _RefusingJobs:
        def submit(self, *_args, **_kwargs):
            raise file_jobs.FilePathError(
                "only local fixed or removable disks are allowed")

    svc = object.__new__(Service)
    svc.file_jobs = _RefusingJobs()
    svc.log = FakeLog()
    answer = svc._transcribe_file_request(
        {"path": "D:\\media\\a.wav", "formats": ["srt"],
         "out": "\\\\server\\share"},
        lambda _event: None)
    assert answer["ok"] is False
    assert answer["code"] == 2


# -- 4. CLI ------------------------------------------------------------
def _summary(folder, **overrides):
    payload = {
        "ok": True, "batch_id": "b1", "folder": str(folder), "out": "",
        "recursive": False, "total": 2, "done": 1, "skipped": 1,
        "failed": 0, "cancelled": 0, "progress": 100.0,
        "files": [
            {"source": str(folder) + "\\a.wav", "relative": "a.wav",
             "state": "done", "outputs": [str(folder) + "\\a.srt"],
             "segments": 2, "duration_s": 12.485, "error": None},
            {"source": str(folder) + "\\b.wav", "relative": "b.wav",
             "state": "skipped", "outputs": [str(folder) + "\\b.srt"],
             "segments": 0, "duration_s": 0.0, "error": None},
        ],
    }
    payload.update(overrides)
    return payload


def test_cli_transcribe_folder_json_and_exit_codes(tmp_path):
    from vibeforma import app as app_mod

    def _run(answer, argv=None):
        out, err = io.StringIO(), io.StringIO()

        def fake(_payload, on_event=None):
            if on_event is not None:
                on_event({"event": "progress", "total": 2, "done": 0,
                          "skipped": 0, "failed": 0, "cancelled": 0,
                          "progress": 0.0})
            return answer

        code = app_mod.run_transcribe_folder(
            argv or ["--transcribe-folder", str(tmp_path), "--json"],
            out=out, err=err, client=fake)
        return code, out.getvalue(), err.getvalue()

    summary = _summary(tmp_path)
    code, out, err = _run({"event": "done", "ok": True, "batch_id": "b1",
                           "summary": summary})
    assert code == 0
    parsed = json.loads(out)
    assert parsed["total"] == 2 and parsed["done"] == 1
    assert "batch 0/2" in err
    assert "1 done, 1 skipped, 0 failed" in err
    assert "batch " not in out

    failed = _summary(tmp_path, ok=False, failed=1,
                      files=summary["files"] + [
                          {"source": "c", "relative": "c.wav",
                           "state": "error", "outputs": [], "segments": 0,
                           "duration_s": 0.0,
                           "error": "decoder failed"}])
    code, out, err = _run({"event": "done", "ok": False,
                           "batch_id": "b1", "summary": failed})
    assert code == 1
    assert json.loads(out)["failed"] == 1
    assert "failed c.wav: decoder failed" in err

    cancelled = _summary(tmp_path, ok=False, cancelled=1)
    code, out, err = _run({"event": "error", "ok": False, "cancelled": True,
                           "code": 3, "summary": cancelled})
    assert code == 3
    assert json.loads(out)["cancelled"] == 1

    code, out, err = _run({"event": "error", "ok": False, "code": 4,
                           "error": "no supported media files"})
    assert code == 4
    assert out == ""
    assert "nothing to transcribe" in err

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_transcribe_folder(
        ["--transcribe-folder", str(tmp_path), "second"], out=out, err=err,
        client=lambda *_a, **_kw: pytest.fail("the client must not be called"))
    assert code == 2
    assert out.getvalue() == ""


def test_cli_transcribe_folder_help_and_bad_arguments(tmp_path):
    from vibeforma import app as app_mod

    text = app_mod.transcribe_folder_help()
    for needle in ("--transcribe-folder", "--cancel-batch", "--recursive",
                   "--out", "--force", "--json", "nothing to transcribe",
                   "Exit codes"):
        assert needle in text
    out, err = io.StringIO(), io.StringIO()
    assert app_mod.run_transcribe_folder(
        ["--transcribe-folder", str(tmp_path), "--help"], out=out,
        err=err) == 0
    assert "--transcribe-folder" in out.getvalue()
    for argv in (["--transcribe-folder"], ["--transcribe-folder", "a", "b"],
                 ["--transcribe-folder", "a", "--bogus"],
                 ["--transcribe-folder", "a", "--format", "pdf"]):
        out, err = io.StringIO(), io.StringIO()
        assert app_mod.run_transcribe_folder(argv, out=out, err=err) == 2


def test_cli_cancel_batch(tmp_path):
    from vibeforma import app as app_mod
    from vibeforma.pipe import PipeUnavailable

    def _run(answer=None, argv=None):
        out, err = io.StringIO(), io.StringIO()

        def fake(_payload):
            if answer is None:
                raise PipeUnavailable("VibeForma is not running")
            return answer

        code = app_mod.run_cancel_batch(
            argv or ["--cancel-batch", "--json"], out=out, err=err,
            client=fake)
        return code, out.getvalue(), err.getvalue()

    code, out, _err = _run({"ok": True, "cancelled": 2})
    assert code == 0
    assert json.loads(out) == {"ok": True, "cancelled": 2}
    code, _out, _err = _run({"ok": True, "cancelled": 0})
    assert code == 1
    code, _out, err = _run(None)
    assert code == 1
    assert "not running" in err
    code, out, _err = _run({"ok": True, "cancelled": 1},
                           argv=["--cancel-batch", "b1", "--json"])
    assert code == 0 and json.loads(out)["cancelled"] == 1


# -- 5. tray and HUD payload ------------------------------------------
class _FakeRoot:
    def withdraw(self):
        pass

    def destroy(self):
        pass


def test_tray_folder_dialog_uses_askdirectory(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import filedialog

    from vibeforma import service as service_mod

    svc = object.__new__(service_mod.Service)
    svc.cfg = {"language": "ru", "file_folder_recursive": True,
               "locale": "en", "dictionary": []}
    svc.log = FakeLog()
    svc.tray = None
    svc.live = {"file_job": {}, "file_batch": {}}
    svc._last_file_outputs = []
    calls = {}

    class _Queue:
        def submit_folder(self, folder, **kwargs):
            calls["folder"] = folder
            calls.update(kwargs)

    svc.file_jobs = _Queue()
    monkeypatch.setattr(tk, "Tk", _FakeRoot)
    monkeypatch.setattr(filedialog, "askdirectory",
                        lambda **_kw: str(tmp_path))
    svc._transcribe_folder_dialog_worker()
    assert calls["folder"] == str(tmp_path)
    assert calls["trusted"] is True
    assert calls["recursive"] is True
    assert calls["formats"] == ["srt", "txt"]
    assert calls["language"] == "ru"


def _locale(lang):
    with open(os.path.join(LOCALES, lang + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _placeholders(value):
    return {name for _, name, _, _ in Formatter().parse(str(value)) if name}


def test_batch_hud_payload_and_locale_keys():
    from file_jobs import FileBatch
    from vibeforma.service import Service

    svc = object.__new__(Service)
    svc.live = {"file_job": {}, "file_batch": {}}
    svc.tray = None
    svc.cfg = {"locale": "en", "dictionary": []}
    svc._last_file_outputs = []
    batch = FileBatch(id="b1", root="D:\\media", total=4, state="running",
                      progress=33.0, current="c1", current_name="sub/b.wav",
                      created_at=1.0)
    svc._on_batch_event(batch)
    payload = svc.live["file_batch"]
    for key in ("id", "state", "total", "done", "skipped", "failed",
                "cancelled", "pending", "progress", "current",
                "current_name", "current_progress", "error", "finished_at",
                "batch_id"):
        assert key in payload
    assert payload["current_name"] == "sub/b.wav"
    assert payload["total"] == 4
    en, ru = _locale("en"), _locale("ru")
    for key in ("tray.transcribe_folder", "tray.cancel_batch",
                "dialog.transcribe_folder", "hud.file_batch",
                "notify.batch_done", "notify.batch_failed",
                "notify.batch_cancelled", "notify.folder_empty",
                "notify.folder_failed"):
        assert key in en and key in ru
        assert _placeholders(en[key]) == _placeholders(ru[key])


def test_folder_batch_one_file_at_a_time(tmp_path):
    first_folder = tmp_path / "first"
    second_folder = tmp_path / "second"
    _make(first_folder, ["one.wav"])
    _make(second_folder, ["two.wav"])
    queue = _jobs(asr=_StubASR(delay=0.05))
    try:
        first = queue.submit_folder(str(first_folder), formats=["srt"])
        second = queue.submit_folder(str(second_folder), formats=["srt"])
        assert _wait_batch(queue, first) == "done"
        assert _wait_batch(queue, second) == "done"
        one = queue.get(first.children[0])
        two = queue.get(second.children[0])
        assert one.started_at is not None and one.finished_at is not None
        assert two.started_at >= one.finished_at
        single = queue.submit(str(first_folder / "one.wav"), formats=["srt"],
                              force=True)
        deadline = time.time() + 10
        while time.time() < deadline and single.state != "done":
            time.sleep(0.02)
        assert single.state == "done"
        assert single.batch_id == "" and single.relative == ""
    finally:
        queue.shutdown()


def test_pipe_line_limit_fits_a_large_batch():
    from vibeforma import pipe

    files = [{"source": f"D:\\media\\folder{i:04d}\\clip{i:04d}.mp4",
              "relative": f"clip{i:04d}.mp4",
              "state": "done",
              "outputs": [f"D:\\media\\folder{i:04d}\\clip{i:04d}.srt"],
              "segments": 12, "duration_s": 83.4, "error": None}
             for i in range(2000)]
    summary = {"ok": True, "batch_id": "b1", "folder": "D:\\media",
               "out": "", "recursive": True, "total": len(files),
               "done": len(files), "skipped": 0, "failed": 0,
               "cancelled": 0, "progress": 100.0, "files": files}
    line = json.dumps(summary, ensure_ascii=False).encode("utf-8")
    assert len(line) < pipe.MAX_LINE
    assert pipe.MAX_LINE >= 4 * 1024 * 1024
    short = json.dumps({"ok": True, "total": 0, "files": []})
    assert len(short) < pipe.MAX_LINE


# -- 6. HUD view (Tk-free) --------------------------------------------
def test_file_progress_view_single_file_queued_running_done():
    import hud

    now = time.time()
    st = {"file_job": {"id": "j1", "batch_id": "", "state": "queued",
                       "progress": 0.0, "name": "a.wav", "finished_at": 0.0}}
    view = hud.file_progress_view(st, now=now)
    assert view["visible"] is True
    assert view["indeterminate"] is True
    assert view["percent"] == 0
    assert view["state"] == "queued"
    assert view["failed"] == 0

    st["file_job"].update(state="running", progress=0.0)
    view = hud.file_progress_view(st, now=now)
    assert view["indeterminate"] is True

    st["file_job"].update(state="running", progress=42.0)
    view = hud.file_progress_view(st, now=now)
    assert view["indeterminate"] is False
    assert view["title"] == "a.wav"
    assert view["percent"] == 42
    assert view["overall_percent"] == 42
    assert view["overall_text"] == ""

    st["file_job"].update(state="done", progress=100.0, finished_at=now)
    view = hud.file_progress_view(st, now=now)
    assert view["state"] == "done"
    assert view["terminal"] is True
    assert view["percent"] == 100
    assert view["failed"] == 0
    later = hud.file_progress_view(
        st, now=now + hud.FILE_PANEL_HOLD_S + 1)
    assert later["visible"] is False


def test_file_progress_view_batch_counts_current_and_error():
    import hud

    now = 1000.0
    st = {"file_batch": {
        "id": "b1", "state": "running", "total": 12, "done": 2,
        "skipped": 1, "failed": 1, "cancelled": 0, "pending": 8,
        "progress": 33.0, "current": "c1", "current_name": "sub/b.wav",
        "current_progress": 42.0,
        "error": "sub/bad.wav: decoder failed", "finished_at": 0.0}}
    view = hud.file_progress_view(st, now=now)
    assert view["batch"] is True
    # The formula (done + skipped + failed + cancelled) wins over the typo in
    # the spec case list: 2 + 1 + 1 = 4 files complete of 12.
    assert view["complete"] == 4
    assert view["overall_text"] == "4/12"
    assert view["overall_percent"] == 33
    assert view["percent"] == 42
    assert view["title"] == "sub/b.wav"
    assert view["failed"] == 1
    assert view["error"] == "sub/bad.wav: decoder failed"

    st["file_batch"].update(state="done", current="",
                            current_name="sub/b.wav",
                            progress=100.0, finished_at=now)
    done_view = hud.file_progress_view(st, now=now)
    assert done_view["state"] == "error"
    assert done_view["title"] == "sub/b.wav"
    assert done_view["failed"] == 1
    st["file_batch"]["failed"] = 0
    assert hud.file_progress_view(st, now=now)["state"] == "done"
    assert hud.file_progress_view(st, now=now)["failed"] == 0


def test_file_progress_view_paused_and_batch_child_not_single():
    import hud

    now = time.time()
    st = {"file_job": {"id": "j1", "batch_id": "", "state": "paused",
                       "progress": 40.0, "name": "a.wav", "finished_at": 0.0}}
    view = hud.file_progress_view(st, now=now)
    assert view["paused"] is True
    assert view["state"] == "paused"
    assert view["percent"] == 40

    st = {"file_batch": {"id": "b1", "state": "running", "total": 2,
                         "done": 0, "skipped": 0, "failed": 0,
                         "cancelled": 0, "progress": 10.0, "current": "c1",
                         "current_name": "parent.wav", "current_progress": 20.0,
                         "error": "", "finished_at": 0.0},
          "file_job": {"id": "c1", "batch_id": "b1", "state": "running",
                       "progress": 90.0, "name": "child.wav",
                       "finished_at": 0.0}}
    view = hud.file_progress_view(st, now=now)
    assert view["batch"] is True
    assert view["title"] == "parent.wav"
    assert view["percent"] == 20

    st = {"file_batch": {"id": "b2", "state": "done", "total": 1,
                         "done": 1, "skipped": 0, "failed": 0,
                         "cancelled": 0, "progress": 100.0, "current": "",
                         "current_name": "", "current_progress": 0.0,
                         "error": "", "finished_at": now},
          "file_job": {"id": "j2", "batch_id": "", "state": "running",
                       "progress": 12.0, "name": "solo.wav",
                       "finished_at": 0.0}}
    view = hud.file_progress_view(st, now=now)
    assert view["batch"] is False
    assert view["title"] == "solo.wav"
    assert view["percent"] == 12


def test_file_panel_locale_keys():
    en, ru = _locale("en"), _locale("ru")
    keys = ["hud.file_panel_title", "hud.file_state_queued",
            "hud.file_state_running", "hud.file_state_paused",
            "hud.file_state_done", "hud.file_state_error",
            "hud.file_state_cancelled"]
    for key in keys:
        assert key in en and key in ru
    for token in ("{state}", "{name}"):
        assert token in en["hud.file_panel_title"]
        assert token in ru["hud.file_panel_title"]
    assert _placeholders(en["hud.file_panel_title"]) \
        == _placeholders(ru["hud.file_panel_title"])


@pytest.mark.skipif(os.name != "nt", reason="the HUD is a Windows window")
def test_file_panel_widget_packs_shows_and_hides():
    import hud as hud_mod

    class _FakeService:
        def __init__(self):
            self.live = {"state": "idle", "file_job": {}, "file_batch": {},
                         "level": 0.0, "partial": "", "last": "",
                         "last_ts": 0.0, "privacy": False}
            self.cfg = {"hud_alpha": 1.0, "hud_autogrow": True, "hotkey": ""}
            self.pending_update = None
            self.panel_request = threading.Event()

        def tr(self, key, **kwargs):
            pairs = " ".join(f"{name}={value}" for name, value in kwargs.items())
            return f"{key} {pairs}".strip()

    svc = _FakeService()
    w = hud_mod.Hud(svc, FakeLog())
    try:
        w.root.update()
        before = w.root.winfo_height()
        svc.live["file_job"] = {
            "id": "j1", "batch_id": "", "state": "running",
            "progress": 42.0, "name": "long_ru.mp4", "error": "",
            "finished_at": 0.0,
        }
        w._sync_file_panel(dict(svc.live))
        w.root.update()
        assert w.file_panel.winfo_manager() == "pack"
        assert w.file_panel.winfo_ismapped() == 1
        assert "name=long_ru.mp4" in w.file_title.cget("text")
        assert w.file_pct.cget("text") == "42%"
        assert w._file_h == w._px(hud_mod.FILE_PANEL_H)
        assert w.root.winfo_height() == before + w._px(hud_mod.FILE_PANEL_H)

        svc.live["file_job"] = {}
        svc.live["file_batch"] = {
            "id": "b1", "state": "running", "total": 12, "done": 2,
            "skipped": 1, "failed": 1, "cancelled": 0, "pending": 8,
            "progress": 33.0, "current": "c1", "current_name": "sub/b.wav",
            "current_progress": 42.0, "error": "", "finished_at": 0.0,
        }
        w._sync_file_panel(dict(svc.live))
        w.root.update()
        assert "name=sub/b.wav" in w.file_title.cget("text")
        assert w.file_pct.cget("text") == "42%"
        overall = w.file_overall.cget("text")
        for token in ("done=4", "total=12", "pct=33", "failed=1"):
            assert token in overall

        svc.live["file_batch"].update(
            state="done", current="", current_name="sub/b.wav",
            progress=100.0,
            finished_at=time.time() - hud_mod.FILE_PANEL_HOLD_S - 1)
        w._sync_file_panel(dict(svc.live))
        w.root.update()
        assert w.file_panel.winfo_manager() == ""
        assert w._file_h == 0
        assert w.root.winfo_height() == before
    finally:
        w.root.destroy()


def test_file_panel_bar_moves_while_running_has_no_percent(monkeypatch):
    import hud as hud_mod

    class _Bar:
        def __init__(self):
            self.calls = []
            self.deleted = 0

        def delete(self, *_args):
            self.deleted += 1

        def winfo_width(self):
            return 100

        def winfo_height(self):
            return 4

        def create_rectangle(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    w = object.__new__(hud_mod.Hud)
    w._px = lambda value: int(value)
    w.file_bar = _Bar()
    st = {"file_job": {"id": "j1", "batch_id": "", "state": "running",
                       "progress": 0.0, "name": "a.wav", "error": "",
                       "finished_at": 0.0}}
    view = hud_mod.file_progress_view(st, now=0.0)
    assert view["indeterminate"] is True

    monkeypatch.setattr(hud_mod.time, "time",
                        lambda: hud_mod.FILE_BAR_MS * 0.25 / 1000.0)
    w._paint_file_bar(view)
    first = w.file_bar.calls[-1][0][0]
    w.file_bar.calls.clear()
    monkeypatch.setattr(hud_mod.time, "time",
                        lambda: hud_mod.FILE_BAR_MS * 0.75 / 1000.0)
    w._paint_file_bar(view)
    second = w.file_bar.calls[-1][0][0]
    assert first != second
    assert w.file_bar.deleted == 2

    st["file_job"]["progress"] = 42.0
    view = hud_mod.file_progress_view(st, now=0.0)
    assert view["indeterminate"] is False
    w.file_bar.calls.clear()
    monkeypatch.setattr(hud_mod.time, "time",
                        lambda: hud_mod.FILE_BAR_MS * 0.25 / 1000.0)
    w._paint_file_bar(view)
    flat_first = w.file_bar.calls[-1][0]
    w.file_bar.calls.clear()
    monkeypatch.setattr(hud_mod.time, "time",
                        lambda: hud_mod.FILE_BAR_MS * 0.75 / 1000.0)
    w._paint_file_bar(view)
    flat_second = w.file_bar.calls[-1][0]
    assert flat_first == flat_second
    assert flat_first[0] == 0 and flat_first[2] == 42.0
