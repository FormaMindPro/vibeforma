"""File mode: PyAV decode, segments, formats, queue, API, CLI, launcher, pipe."""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import struct
import time
import wave
from pathlib import Path

import pytest
import transcript
from transcript import Segment, Word

np = pytest.importorskip("numpy")
pytest.importorskip("av")

ROOT = Path(__file__).resolve().parents[1]
TEST_WAV = ROOT / "test_ru.wav"
TOKEN = "test-token-file-mode"  # noqa: S105 - a test value, not a secret


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


# -- formats -----------------------------------------------------------
def test_formats_txt_json_srt_vtt():
    segments = _segments() + [Segment(12.0, 12.0, "   ")]
    meta = {"language": "ru", "model": "large-v3-turbo", "duration": 12.485}

    txt = transcript.to_txt(segments)
    assert txt == "first phrase\n\nsecond phrase\n"

    payload = json.loads(transcript.to_json(segments, meta))
    assert payload["text"] == "first phrase second phrase"
    assert payload["language"] == "ru"
    assert payload["duration"] == 12.485
    assert payload["segments"][0] == {"start": 0.26, "end": 6.7,
                                      "text": "first phrase"}

    srt = transcript.to_srt(segments)
    assert srt.startswith("1\n00:00:00,260 --> 00:00:06,700\n")
    assert "2\n00:00:06,700 --> 00:00:11,660" in srt
    assert "12.0" not in srt

    vtt = transcript.to_vtt(segments)
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:00.260 --> 00:00:06.700" in vtt


def test_format_timestamps_are_monotonic_and_non_negative():
    rows = [Segment(-5.0, -1.0, "a"), Segment(0.5, 0.2, "b")]
    srt = transcript.to_srt(rows)
    assert "00:00:00,000 --> 00:00:00,000" in srt
    assert "00:00:00,500 --> 00:00:00,500" in srt


def test_format_keeps_a_break_inside_a_cue():
    srt = transcript.to_srt([Segment(0, 1, "one\ntwo")])
    body = srt.split("\n")
    assert body[2] == "one"
    assert body[3] == "two"


def test_json_carries_words_when_present():
    segment = Segment(0, 1, "hi", words=[Word(0.0, 0.5, "hi")])
    payload = json.loads(transcript.to_json([segment], {}))
    assert payload["segments"][0]["words"] == [
        {"start": 0.0, "end": 0.5, "text": "hi"}]


def test_unknown_format_is_refused():
    with pytest.raises(ValueError):
        transcript.render(_segments(), "pdf")


# -- own PyAV decoder --------------------------------------------------
@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_own_decoder_decodes_the_real_wav_without_faster_whisper():
    import asr
    fw_audio = pytest.importorskip("faster_whisper.audio")

    def _explode(*_args, **_kwargs):
        raise AssertionError("faster_whisper.audio.decode_audio must not run")

    original = fw_audio.decode_audio
    fw_audio.decode_audio = _explode
    try:
        audio, duration = asr.decode_audio_file(str(TEST_WAV))
    finally:
        fw_audio.decode_audio = original

    assert audio.dtype.name == "float32"
    assert audio.shape == (199760,)
    assert duration == pytest.approx(12.485, abs=0.01)


class _FakeModel:
    """Stands in for WhisperModel: records the array, yields segments."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else _segments()
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))

        def _rows():
            for row in self.rows:
                yield _Row(row)

        return _rows(), _Row(None, language="ru", duration=12.0)


class _Row:
    """A tiny stand-in for faster-whisper's segment and info objects."""

    def __init__(self, segment, **extra):
        self.start = getattr(segment, "start", 0.0)
        self.end = getattr(segment, "end", 0.0)
        self.text = getattr(segment, "text", "")
        self.words = getattr(segment, "words", None)
        for name in ("avg_logprob", "compression_ratio", "no_speech_prob",
                     "temperature"):
            setattr(self, name, getattr(segment, name, None))
        for key, value in extra.items():
            setattr(self, key, value)


def _fake_asr(rows=None):
    import asr
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, FakeLog())
    engine.model = _FakeModel(rows)
    return engine


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_transcribe_file_feeds_an_array_and_yields_segments():
    import asr

    engine = _fake_asr()
    progress = []

    segments = list(engine.transcribe_file(str(TEST_WAV), language="ru",
                                           on_progress=progress.append))

    audio, kwargs = engine.model.calls[0]
    assert audio.shape == (199760,)
    assert kwargs["vad_filter"] is True
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["word_timestamps"] is False
    assert kwargs["hallucination_silence_threshold"] == (
        asr.hallucination.HALLUCINATION_SILENCE_S)
    # The ratio threshold is calibrated on one-byte text; Cyrillic keeps the
    # engine's default and is guarded by the post-filter.
    assert "compression_ratio_threshold" not in kwargs
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.26, 6.7, "first phrase"), (6.7, 11.66, "second phrase")]
    assert progress[-1] == 100.0


def test_file_mode_drops_invented_text_on_silence():
    engine = _fake_asr([
        Segment(0.0, 1.0, " first phrase"),
        # "subscribe to the channel", the phrase the card names.
        Segment(1.0, 2.0,
                " \u041f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439"
                "\u0442\u0435\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b",
                no_speech_prob=0.92, compression_ratio=1.1),
        Segment(2.0, 3.0, " second phrase", no_speech_prob=0.02,
                compression_ratio=1.2),
    ])

    segments = list(engine.transcribe_array(
        np.zeros((int(3.0 * 16000),), dtype=np.float32), 3.0))

    assert [s.text for s in segments] == ["first phrase", "second phrase"]
    assert any("Dropped invented text" in line
               for _level, line in engine.log.lines)


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_should_pause_holds_between_segments_and_resumes():
    import threading

    engine = _fake_asr()
    paused = {"on": True}
    threading.Timer(0.3, lambda: paused.update(on=False)).start()

    started = time.monotonic()
    segments = list(engine.transcribe_file(
        str(TEST_WAV), language="ru", should_pause=lambda: paused["on"]))
    waited = time.monotonic() - started

    assert [s.text for s in segments] == ["first phrase", "second phrase"]
    assert waited >= 0.25


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_should_stop_cancels_before_decoding():
    import asr

    engine = _fake_asr()
    stream = engine.transcribe_file(str(TEST_WAV), should_stop=lambda: True)
    with pytest.raises(asr.AsrCancelled):
        next(stream)


# -- job queue ---------------------------------------------------------
class _SlowASR:
    """A file engine that can be held between segments by the test."""

    def __init__(self, rows=None, delay=0.0):
        self.rows = rows or _segments()
        self.delay = delay
        self.started = 0

    def transcribe_file(self, _path, **kwargs):
        self.started += 1
        if kwargs.get("on_duration"):
            kwargs["on_duration"](12.485)
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


def _jobs(cfg=None, asr=None):
    import file_jobs
    config = {"language": "ru", "model": "fake", "file_max_mb": 4096,
              "job_keep_s": 3600, "file_roots": [], "file_word_timestamps": False}
    config.update(cfg or {})
    return file_jobs.FileJobs(config, asr or _SlowASR(), FakeLog())


def _wait(job, state="done", timeout=5.0):
    import file_jobs
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in file_jobs.TERMINAL:
            return job.state
        time.sleep(0.02)
    return job.state


def test_file_jobs_fifo_one_running_and_progress(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    queue = _jobs()
    try:
        first = queue.submit(str(source), formats=["srt"])
        second = queue.submit(str(source), formats=["txt"])
        assert _wait(first) == "done"
        assert _wait(second) == "done"
        assert first.progress == 100.0
        assert first.started_at is not None and second.started_at is not None
        assert second.started_at >= first.started_at
        assert (tmp_path / "clip.srt").is_file()
        assert (tmp_path / "clip.txt").is_file()
    finally:
        queue.shutdown()


def test_file_jobs_detailed_progress_is_reported(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    seen = []
    queue = _jobs(asr=_SlowASR(delay=0.05))
    try:
        job = queue.submit(str(source))
        deadline = time.time() + 3
        while time.time() < deadline and job.state != "done":
            seen.append(queue.get(job.id).progress)
            time.sleep(0.01)
        assert seen
        assert max(seen) > 0
        assert job.duration == pytest.approx(12.485)
    finally:
        queue.shutdown()


def test_file_jobs_pause_and_resume_around_a_dictation(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    asr = _SlowASR(delay=0.05)
    queue = _jobs(asr=asr)
    try:
        job = queue.submit(str(source))
        deadline = time.time() + 5
        while time.time() < deadline and job.state != "running":
            time.sleep(0.02)
        assert job.state == "running", job.state
        queue.dictation_active(True)
        deadline = time.time() + 5
        while time.time() < deadline and job.state != "paused":
            time.sleep(0.02)
        assert job.state == "paused", job.state
        queue.dictation_active(False)
        deadline = time.time() + 5
        while time.time() < deadline and job.state != "running":
            time.sleep(0.02)
        assert job.state in ("running", "done")
        assert _wait(job) == "done"
    finally:
        queue.shutdown()


def test_file_jobs_cancel_is_cooperative(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    asr = _SlowASR(delay=0.05)
    queue = _jobs(asr=asr)
    try:
        job = queue.submit(str(source))
        deadline = time.time() + 5
        while time.time() < deadline and job.state != "running":
            time.sleep(0.02)
        assert queue.cancel(job.id) is True
        assert _wait(job) == "cancelled"
        assert queue.cancel(job.id) is False
    finally:
        queue.shutdown()


def test_file_jobs_result_and_no_overwrite_without_force(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    queue = _jobs()
    try:
        job = queue.submit(str(source), formats=["srt"])
        assert _wait(job) == "done"
        assert "first phrase" in queue.result(job.id, "srt")
        blocked = queue.submit(str(source), formats=["srt"])
        assert _wait(blocked) == "error"
        assert "already exists" in (blocked.error or "")
        forced = queue.submit(str(source), formats=["srt"], force=True)
        assert _wait(forced) == "done"
    finally:
        queue.shutdown()


class _RecordingASR(_SlowASR):
    """A file engine that keeps the options the job handed it."""

    def __init__(self):
        super().__init__()
        self.last: dict = {}

    def transcribe_file(self, path, **kwargs):
        self.last = dict(kwargs)
        yield from super().transcribe_file(path, **kwargs)


def test_file_job_carries_prompt_words_and_clip_to_the_engine(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    asr = _RecordingASR()
    queue = _jobs(asr=asr)
    try:
        job = queue.submit(str(source), prompt="names: Forma",
                           word_timestamps=True, clip_timestamps="2, 5")
        assert _wait(job) == "done"
        assert asr.last["initial_prompt"] == "names: Forma"
        assert asr.last["word_timestamps"] is True
        assert asr.last["clip_timestamps"] == "2,5"

        plain = queue.submit(str(source), formats=["txt"])
        assert _wait(plain) == "done"
        # No prompt means the engine keeps the configured preset; no flag
        # means the configured word-timestamp setting; no clip means the file.
        assert asr.last["initial_prompt"] is None
        assert asr.last["word_timestamps"] is False
        assert asr.last["clip_timestamps"] is None

        with pytest.raises(ValueError):
            queue.submit(str(source), clip_timestamps="5,2")
        with pytest.raises(ValueError):
            queue.submit(str(source), clip_timestamps="two,5")
    finally:
        queue.shutdown()


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_file_job_without_prompt_keeps_the_configured_preset(tmp_path):
    engine = _fake_asr()
    queue = _jobs(asr=engine)
    try:
        job = queue.submit(str(TEST_WAV))
        assert _wait(job) == "done"
        _audio, kwargs = engine.model.calls[0]
        assert kwargs["initial_prompt"]   # the configured language preset

        engine.model = _FakeModel()
        hinted = queue.submit(str(TEST_WAV), prompt="names: Forma")
        assert _wait(hinted) == "done"
        _audio, kwargs = engine.model.calls[0]
        assert kwargs["initial_prompt"] == "names: Forma"
    finally:
        queue.shutdown()


def test_file_jobs_refuse_paths_outside_the_roots(tmp_path):
    import file_jobs
    outside = tmp_path / "root" / "clip.wav"
    outside.parent.mkdir()
    outside.write_bytes(b"RIFF")
    other = tmp_path / "other"
    other.mkdir()
    queue = file_jobs.FileJobs({"file_roots": [str(other)], "job_keep_s": 60},
                               _SlowASR(), FakeLog())
    try:
        with pytest.raises(file_jobs.FilePathError):
            queue.submit(str(outside))
        with pytest.raises(file_jobs.FilePathError):
            queue.submit("\\\\server\\share\\clip.wav")
        inside = tmp_path / "other" / "ok.wav"
        inside.write_bytes(b"RIFF")
        job = queue.submit(str(inside))
        assert _wait(job) == "done"
    finally:
        queue.shutdown()


# -- HTTP API ----------------------------------------------------------
class _FakeASR:
    def __init__(self):
        self.calls = []
        self.raise_error = False

    def transcribe(self, _audio):
        return "hello"

    def transcribe_array(self, _audio, _duration, language=None,
                         initial_prompt=None, temperature=None, **kwargs):
        self.calls.append({"language": language,
                           "initial_prompt": initial_prompt,
                           "temperature": temperature,
                           **kwargs})
        if self.raise_error:
            raise RuntimeError("engine failure")
        return _segments()

    def transcribe_file(self, _path, **kwargs):
        if kwargs.get("on_duration"):
            kwargs["on_duration"](12.485)
        if self.raise_error:
            raise RuntimeError("engine failure")
        yield from _segments()


class _FakeService:
    def __init__(self):
        self.state = "idle"
        self.asr = _FakeASR()
        self.file_jobs = None


def _http(server, method, path, body=None, headers=None, token=TOKEN):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    all_headers = dict(headers or {})
    if token is not None:
        all_headers.setdefault("X-VibeForma-Token", token)
    try:
        conn.request(method, path, body=body, headers=all_headers)
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    import main
    import vibeforma.http_api as http_api
    monkeypatch.setattr(http_api, "LOG_DIR", str(tmp_path))
    cfg = {"http_port": 0, "http_token": TOKEN, "model": "fake",
           "language": "ru", "file_max_sync_s": 900, "file_max_mb": 64}
    service = _FakeService()
    service.file_jobs = _jobs(cfg=cfg, asr=service.asr)
    httpd = main.start_http_api(cfg, service, FakeLog())
    assert httpd is not None
    yield httpd, service, cfg
    if service.file_jobs is not None:
        service.file_jobs.shutdown()
    httpd.shutdown()
    httpd.server_close()


def _multipart(fields, boundary="----vibeforma-test"):
    body = b""
    for name, (filename, payload) in fields.items():
        body += f"--{boundary}\r\n".encode()
        if filename:
            body += (f'Content-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\n'
                     "Content-Type: application/octet-stream\r\n\r\n").encode()
        else:
            body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     ).encode()
        body += payload + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _wav_bytes(seconds=0.2, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = b"".join(struct.pack("<h", 0) for _ in range(int(rate * seconds)))
        handle.writeframes(frames)
    return buffer.getvalue()


def test_http_capabilities_and_health(server):
    httpd, _service, _cfg = server
    status, body, _ = _http(httpd, "GET", "/capabilities")
    caps = json.loads(body)
    assert status == 200
    assert caps["file_mode"] is True
    assert caps["formats"] == ["txt", "json", "srt", "vtt"]
    assert caps["max_file_mb"] == 64
    assert caps["dictation_priority"] is True

    status, body, _ = _http(httpd, "GET", "/health")
    health = json.loads(body)
    assert status == 200
    assert health["file_mode"] is True
    assert health["jobs_running"] == 0
    assert health["jobs_queued"] == 0


def test_http_openai_json_text_srt_vtt(server):
    httpd, _service, _cfg = server
    for fmt, needle in (("json", b'"text"'), ("text", b"first phrase"),
                        ("srt", b"00:00:00,260"), ("vtt", b"WEBVTT")):
        body, ctype = _multipart({
            "file": ("clip.wav", _wav_bytes()),
            "response_format": ("", fmt.encode()),
            "language": ("", b"ru"),
        })
        status, payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                                   body=body, headers={"Content-Type": ctype})
        assert status == 200, payload
        assert needle in payload


def test_http_openai_accepts_bearer_and_token_headers(server):
    httpd, _service, _cfg = server
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
    status, _payload, _ = _http(
        httpd, "POST", "/v1/audio/transcriptions", body=body,
        headers={"Content-Type": ctype}, token=None)
    assert status == 401
    status, _payload, _ = _http(
        httpd, "POST", "/v1/audio/transcriptions", body=body,
        headers={"Content-Type": ctype, "Authorization": "Bearer " + TOKEN},
        token=None)
    assert status == 200


def test_http_openai_refuses_a_file_over_the_sync_limit(server):
    httpd, _service, cfg = server
    cfg["file_max_sync_s"] = 0.05
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes(2.0))})
    status, payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                               body=body, headers={"Content-Type": ctype})
    assert status == 413
    assert b"/jobs" in payload


def test_http_jobs_post_get_cancel_and_result(server, tmp_path):
    httpd, service, _cfg = server
    source = tmp_path / "clip.wav"
    source.write_bytes(_wav_bytes(0.2))
    payload = json.dumps({"path": str(source), "formats": []}).encode()
    status, body, headers = _http(httpd, "POST", "/jobs", body=payload,
                                  headers={"Content-Type": "application/json"})
    assert status == 202
    job_id = json.loads(body)["job_id"]
    assert headers.get("Location") == "/jobs/" + job_id

    status, body, _ = _http(httpd, "GET", "/jobs/" + job_id)
    assert status == 200
    assert json.loads(body)["id"] == job_id

    deadline = time.time() + 5
    state = ""
    while time.time() < deadline:
        _status, body, _ = _http(httpd, "GET", "/jobs/" + job_id)
        state = json.loads(body)["state"]
        if state in ("done", "error", "cancelled"):
            break
        time.sleep(0.02)
    assert state == "done"
    status, body, headers = _http(httpd, "GET",
                                  "/jobs/" + job_id + "/result?format=srt")
    assert status == 200
    assert b"00:00:00,260" in body


def test_http_job_result_is_409_until_done(server, tmp_path):
    httpd, service, _cfg = server
    source = tmp_path / "clip.wav"
    source.write_bytes(_wav_bytes(0.2))

    class Slow(_SlowASR):
        def transcribe_file(self, path, **kwargs):
            time.sleep(0.4)
            yield from super().transcribe_file(path, **kwargs)

    service.asr = Slow()
    service.file_jobs.shutdown()
    service.file_jobs = _jobs(cfg=dict(_cfg), asr=service.asr)
    payload = json.dumps({"path": str(source), "formats": []}).encode()
    status, body, _ = _http(httpd, "POST", "/jobs", body=payload,
                            headers={"Content-Type": "application/json"})
    assert status == 202
    job_id = json.loads(body)["job_id"]
    status, _body, _ = _http(httpd, "GET", "/jobs/" + job_id + "/result")
    assert status == 409


def test_http_jobs_delete_cancels(server, tmp_path):
    httpd, service, _cfg = server
    source = tmp_path / "clip.wav"
    source.write_bytes(_wav_bytes(0.2))

    class Slow(_SlowASR):
        def transcribe_file(self, path, **kwargs):
            time.sleep(0.3)
            yield from super().transcribe_file(path, **kwargs)

    service.asr = Slow()
    service.file_jobs.shutdown()
    service.file_jobs = _jobs(cfg=dict(_cfg), asr=service.asr)
    payload = json.dumps({"path": str(source)}).encode()
    status, body, _ = _http(httpd, "POST", "/jobs", body=payload,
                            headers={"Content-Type": "application/json"})
    job_id = json.loads(body)["job_id"]
    status, _body, _ = _http(httpd, "DELETE", "/jobs/" + job_id)
    assert status == 200
    status, body, _ = _http(httpd, "GET", "/jobs/" + job_id)
    assert json.loads(body)["state"] == "cancelled"


def test_http_jobs_refuse_a_path_outside_the_roots(server, tmp_path):
    httpd, service, cfg = server
    cfg["file_roots"] = [str(tmp_path / "allowed")]
    service.file_jobs.cfg["file_roots"] = cfg["file_roots"]
    (tmp_path / "clip.wav").write_bytes(_wav_bytes(0.2))
    payload = json.dumps({"path": str(tmp_path / "clip.wav")}).encode()
    status, _body, _ = _http(httpd, "POST", "/jobs", body=payload,
                             headers={"Content-Type": "application/json"})
    assert status == 403


# -- CLI ---------------------------------------------------------------
def test_parse_transcribe_arguments():
    from vibeforma import app as app_mod

    options = app_mod.parse_transcribe_args(
        ["--transcribe", "clip.mp4", "--lang", "ru", "--format", "vtt",
         "--out", "out.vtt", "--json", "--force"])
    assert options["path"] == "clip.mp4"
    assert options["language"] == "ru"
    assert options["formats"] == ["vtt"]
    assert options["out"] == "out.vtt"
    assert options["json"] is True
    assert options["force"] is True

    default = app_mod.parse_transcribe_args(["clip.mp4"])
    assert default["formats"] == ["srt"]

    assert app_mod.parse_transcribe_args(["--transcribe"])["path"] == ""
    with pytest.raises(app_mod.CliArgumentError):
        app_mod.parse_transcribe_args(["clip.mp4", "--format", "pdf"])
    with pytest.raises(app_mod.CliArgumentError):
        app_mod.parse_transcribe_args(["a.mp4", "b.mp4"])
    with pytest.raises(app_mod.CliArgumentError):
        app_mod.parse_transcribe_args(["--nope"])


def test_cli_help_is_printable():
    from vibeforma import app as app_mod

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli("--transcribe", argv=["--help"], out=out, err=err)
    assert code == 0
    assert "run.cmd --transcribe" in out.getvalue()
    assert "Exit codes" in out.getvalue()
    assert err.getvalue() == ""


def test_cli_transcribe_json_answer_and_exit_zero():
    from vibeforma import app as app_mod

    seen = {}

    def client(payload, on_event=None):
        seen.update(payload)
        if on_event:
            on_event({"event": "progress", "progress": 42.0})
            on_event({"event": "progress", "progress": 100.0})
        return {"event": "done", "ok": True, "job_id": "abc",
                "out": ["C:\\out\\clip.srt"], "segments": 2,
                "duration_s": 12.5}

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli("--transcribe",
                           argv=["--transcribe", "clip.mp4", "--format", "srt",
                                 "--json", "--lang", "ru"],
                           out=out, err=err, client=client)
    assert code == 0
    answer = json.loads(out.getvalue())
    assert answer["ok"] is True
    assert answer["segments"] == 2
    assert "42%" in err.getvalue()
    assert "100%" in err.getvalue()
    assert seen["formats"] == ["srt"]
    assert seen["language"] == "ru"


def test_cli_progress_is_monotonic_and_duplicate_free():
    from vibeforma import app as app_mod

    def client(_payload, on_event=None):
        if on_event:
            for _ in range(10_000):
                on_event({"event": "progress", "progress": 0.0})
        return {"event": "done", "ok": True, "job_id": "abc",
                "out": ["C:\\out\\clip.srt"], "segments": 2,
                "duration_s": 12.5}

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_transcribe(
        ["--transcribe", "clip.mp4", "--format", "srt", "--json"],
        out=out, err=err, client=client)
    assert code == 0
    percentages = [int(m) for m in re.findall(r"VibeForma: (\d+)%",
                                              err.getvalue())]
    assert len(percentages) <= 101
    assert len(percentages) == len(set(percentages))
    assert percentages == sorted(percentages)
    assert percentages[-1] == 100
    assert json.loads(out.getvalue())["ok"] is True


def test_cli_survives_a_closed_stderr_pipe():
    from vibeforma import app as app_mod

    class BrokenErr:
        def write(self, _text):
            raise BrokenPipeError("stderr pipe is closed")

        def flush(self):
            raise BrokenPipeError("stderr pipe is closed")

    def client(_payload, on_event=None):
        if on_event:
            on_event({"event": "progress", "progress": 42.0})
        return {"event": "done", "ok": True, "job_id": "abc",
                "out": ["C:\\out\\clip.srt"], "segments": 2,
                "duration_s": 12.5}

    out = io.StringIO()
    code = app_mod.run_transcribe(
        ["--transcribe", "clip.mp4", "--format", "srt", "--json"],
        out=out, err=BrokenErr(), client=client)
    assert code == 0
    answer = json.loads(out.getvalue())
    assert answer["ok"] is True
    assert answer["segments"] == 2


def test_cli_without_a_running_service_exits_one():
    from vibeforma import app as app_mod
    from vibeforma.pipe import PipeUnavailable

    def client(_payload, on_event=None):
        raise PipeUnavailable("VibeForma is not running")

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli("--transcribe", argv=["clip.mp4"], out=out, err=err,
                           client=client)
    assert code == 1
    assert "not running" in err.getvalue()


def test_cli_cancelled_exits_three_and_bad_args_exits_two():
    from vibeforma import app as app_mod

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli("--transcribe", argv=["clip.mp4"], out=out, err=err,
                           client=lambda _p, on_event=None: {
                               "event": "error", "ok": False,
                               "cancelled": True, "error": "cancelled"})
    assert code == 3

    out, err = io.StringIO(), io.StringIO()
    assert app_mod.run_cli("--transcribe", argv=["--bad"], out=out, err=err) == 2


# -- launcher, location, pipe -----------------------------------------
def test_run_cmd_launcher_forwards_cli_arguments():
    launcher = (ROOT / "app" / "run.cmd").read_text(encoding="utf-8")
    assert "python.exe" in launcher
    assert "pythonw.exe" in launcher
    assert "main.py" in launcher
    assert "%*" in launcher


def test_location_json_publishes_launcher_python_entry_and_pipe(tmp_path,
                                                                monkeypatch):
    import vibeforma.http_api as http_api

    target = tmp_path / "VibeForma" / "location.json"
    monkeypatch.setattr(http_api, "location_path", lambda: str(target))
    monkeypatch.setattr(http_api, "CONFIG_PATH", str(tmp_path / "config.json"))
    http_api.publish_location(None, port=49321, api="off",
                              cfg={"file_max_mb": 4096, "file_max_s": 21600,
                                   "file_max_sync_s": 900},
                              pipe_started=True)

    location = json.loads(target.read_text(encoding="utf-8"))
    assert location["schema"] == 2
    assert location["launcher"].endswith("run.cmd")
    assert location["python"].endswith("python.exe")
    assert location["entry"].endswith("main.py")
    assert location["pipe"].startswith("\\\\.\\pipe\\VibeForma-")
    assert location["file_mode"] is True
    assert location["formats"] == ["txt", "json", "srt", "vtt"]
    assert location["max_file_mb"] == 4096
    assert "token" not in json.dumps(location).lower()
    assert not (target.parent / "location.json.tmp").exists()


def test_pipe_name_is_per_user():
    from vibeforma import pipe

    assert pipe.pipe_name("Alice") == "\\\\.\\pipe\\VibeForma-Alice"
    assert pipe.pipe_name("a b") == "\\\\.\\pipe\\VibeForma-a_b"


@pytest.mark.skipif(os.name != "nt", reason="named pipes are Windows-only")
def test_pipe_round_trip_delivers_progress_and_result():
    from vibeforma import pipe

    if not pipe.PIPE_PREFIX:
        pytest.skip("no Windows pipe support")
    name = pipe.pipe_name() + "-test-" + str(os.getpid())
    events = []

    def handler(request, send):
        send({"event": "progress", "progress": 50.0})
        return {"event": "done", "ok": True, "job_id": "1",
                "out": [], "segments": 1, "duration_s": 0.5}

    server = pipe.PipeServer(handler, FakeLog(), name=name)
    assert server.start() is True
    try:
        answer = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                answer = pipe.request({"command": "transcribe"}, name=name,
                                      on_event=events.append)
                break
            except pipe.PipeUnavailable:
                time.sleep(0.02)
        assert answer is not None and answer["ok"] is True
        assert events and events[0]["progress"] == 50.0
    finally:
        server.stop()


@pytest.mark.skipif(os.name != "nt", reason="named pipes are Windows-only")
def test_pipe_client_without_a_service_reports_unavailable():
    from vibeforma import pipe

    name = pipe.pipe_name() + "-absent-" + str(os.getpid())
    with pytest.raises(pipe.PipeUnavailable):
        pipe.request({"command": "transcribe"}, name=name)


def test_config_has_file_mode_limits_and_version():
    import config

    assert config.VERSION == "0.5.0"
    for key in ("file_roots", "file_max_mb", "file_max_s", "file_max_sync_s",
                "job_keep_s", "file_word_timestamps"):
        assert key in config.DEFAULTS
    assert config.DEFAULTS["http_api"] is False

# -- correction attempt 2: readiness, priority, limits, contracts --------
def test_http_503_while_model_loads_and_202_after_readiness(tmp_path, monkeypatch):
    import main
    import vibeforma.http_api as http_api

    monkeypatch.setattr(http_api, "LOG_DIR", str(tmp_path))
    cfg = {"http_port": 0, "http_token": TOKEN, "model": "fake",
           "language": "ru", "file_max_sync_s": 900, "file_max_mb": 64}
    service = _FakeService()
    httpd = main.start_http_api(cfg, service, FakeLog())
    assert httpd is not None
    try:
        source = tmp_path / "clip.wav"
        source.write_bytes(_wav_bytes(0.2))
        payload = json.dumps({"path": str(source)}).encode()
        status, body, headers = _http(
            httpd, "POST", "/jobs", body=payload,
            headers={"Content-Type": "application/json"})
        assert status == 503
        answer = json.loads(body)
        assert answer["ok"] is False and answer["loading"] is True
        assert answer["error"] == "model is loading"
        assert headers.get("Retry-After") == "2"
        assert service.file_jobs is None
        assert not list(tmp_path.glob("_file_job_*"))

        body_form, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
        status, _body, _ = _http(httpd, "POST", "/jobs", body=body_form,
                                 headers={"Content-Type": ctype})
        assert status == 503
        assert not list(tmp_path.glob("_file_job_*"))

        status, _body, _ = _http(httpd, "GET", "/capabilities")
        assert status == 503
        status, body, _ = _http(httpd, "GET", "/health")
        health = json.loads(body)
        assert status == 200
        assert health["model_loading"] is True
        assert health["file_mode"] == "loading"

        service.file_jobs = _jobs(cfg=dict(cfg), asr=service.asr)
        status, body, _ = _http(httpd, "POST", "/jobs", body=payload,
                                headers={"Content-Type": "application/json"})
        assert status == 202
    finally:
        if service.file_jobs is not None:
            service.file_jobs.shutdown()
        httpd.shutdown()
        httpd.server_close()


def test_location_json_omits_the_pipe_until_it_listens(tmp_path, monkeypatch):
    import vibeforma.http_api as http_api

    target = tmp_path / "loc" / "location.json"
    monkeypatch.setattr(http_api, "location_path", lambda: str(target))
    http_api.publish_location(None, port=1, api="off", cfg={})
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "pipe" not in payload
    assert payload["file_mode"] is False

    http_api.publish_location(None, port=1, api="off", cfg={},
                              pipe_started=True)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["pipe"].startswith("\\\\.\\pipe\\VibeForma-")
    assert payload["file_mode"] is True


@pytest.mark.skipif(os.name != "nt", reason="named pipes are Windows-only")
def test_pipe_answers_loading_before_the_model_and_works_after(tmp_path,
                                                              monkeypatch):
    from vibeforma import pipe as pipe_mod
    from vibeforma.service import Service

    # A unique channel name: a live installed service on the user's real pipe
    # must not answer for this test.
    monkeypatch.setenv("USERNAME", "FormaGateTest-" + str(os.getpid()))
    cfg = {"locale": "en", "dictionary": [], "language": "ru",
           "model": "fake", "hotkey": "ctrl+alt+space"}
    service = Service(cfg, FakeLog())
    assert service.start_pipe() is True
    try:
        answer = pipe_mod.request({"command": "transcribe", "path": "x"})
        assert answer["ok"] is False
        assert answer.get("loading") is True
        assert "model is loading" in answer["error"]

        source = tmp_path / "clip.wav"
        source.write_bytes(_wav_bytes(0.2))
        service.file_jobs = _jobs(cfg=dict(cfg), asr=_SlowASR())
        answer = pipe_mod.request({"command": "transcribe",
                                   "path": str(source)})
        assert answer["ok"] is True
    finally:
        if service.file_jobs is not None:
            service.file_jobs.shutdown()
        service.shutdown()


def test_cli_reports_model_loading_not_a_missing_service():
    from vibeforma import app as app_mod

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli(
        "--transcribe", argv=["clip.mp4"], out=out, err=err,
        client=lambda _p, on_event=None: {
            "event": "error", "ok": False, "loading": True,
            "error": "the model is loading"})
    assert code == 1
    assert "the model is loading" in err.getvalue()
    assert "not running" not in err.getvalue()


def test_cli_unknown_model_exits_two():
    from vibeforma import app as app_mod

    out, err = io.StringIO(), io.StringIO()
    code = app_mod.run_cli(
        "--transcribe", argv=["clip.mp4"], out=out, err=err,
        client=lambda _p, on_event=None: {
            "event": "error", "ok": False, "code": 2,
            "error": "model gpt-9 is not installed; this build uses fake"})
    assert code == 2
    assert "is not installed" in err.getvalue()


def test_no_file_job_starts_while_a_dictation_is_active(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"RIFF")
    asr = _SlowASR()
    queue = _jobs(asr=asr)
    try:
        queue.dictation_active(True)
        job = queue.submit(str(source))
        time.sleep(0.5)
        assert job.state == "queued"
        assert asr.started == 0
        queue.dictation_active(False)
        assert _wait(job) == "done"
        assert asr.started == 1
    finally:
        queue.shutdown()


def test_file_mode_processes_blocks_and_frees_the_lock_between_them(monkeypatch):
    import asr

    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, FakeLog())
    calls = []

    class BlockModel:
        def transcribe(self, audio, **_kwargs):
            calls.append(audio.shape[0])

            def rows():
                yield _Row(Segment(0.0, audio.shape[0] / 16000.0, "block"))

            return rows(), _Row(None, language="ru")

    engine.model = BlockModel()
    audio = np.zeros((int(2.5 * 16000),), dtype=np.float32)
    stream = engine.transcribe_array(audio, 2.5)
    first = next(stream)
    acquired = engine._lock.acquire(timeout=1.0)
    if acquired:
        engine._lock.release()
    assert acquired is True
    rest = list(stream)

    assert calls == [16000, 16000, 8000]
    assert max(calls) <= asr.FILE_CHUNK_S * 16000
    assert [round(s.start, 2) for s in [first, *rest]] == [0.0, 1.0, 2.0]


class _FakeStream:
    type = "audio"
    duration = None
    time_base = None


class _FakeResampler:
    def __init__(self, **_kwargs):
        pass

    def resample(self, frame):
        return [frame] if frame is not None else []


class _FakeFrame:
    def __init__(self, samples):
        self._data = np.zeros(samples, dtype=np.float32)

    def to_ndarray(self):
        return self._data


class _FakeContainer:
    def __init__(self, duration_us, frames):
        self.duration = duration_us
        self.streams = [_FakeStream()]
        self.frames = frames
        self.decoded = 0
        self.decode_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def decode(self, _stream):
        self.decode_calls += 1
        for frame in self.frames:
            self.decoded += 1
            yield frame


def test_decoder_refuses_a_declared_duration_over_file_max_s(monkeypatch):
    import asr
    import av

    container = _FakeContainer(int(11 * 1_000_000), [_FakeFrame(16000)])
    monkeypatch.setattr(av, "open", lambda _path: container)
    monkeypatch.setattr(av, "AudioResampler", _FakeResampler)

    with pytest.raises(asr.FileTooLong) as info:
        asr.decode_audio_file(__file__, max_s=10)

    assert container.decode_calls == 0
    assert "file_max_s" in str(info.value)
    assert "10" in str(info.value)


def test_decoder_aborts_an_unknown_duration_at_the_cap(monkeypatch):
    import asr
    import av

    container = _FakeContainer(None, [_FakeFrame(16000)] * 4)
    monkeypatch.setattr(av, "open", lambda _path: container)
    monkeypatch.setattr(av, "AudioResampler", _FakeResampler)

    with pytest.raises(asr.FileTooLong) as info:
        asr.decode_audio_file(__file__, max_s=2.0)

    assert container.decoded == 3
    assert "file_max_s" in str(info.value)


def test_file_job_applies_file_max_s_from_config(tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(_wav_bytes(2.0))
    engine = _fake_asr()
    queue = _jobs(cfg={"file_max_s": 1}, asr=engine)
    try:
        job = queue.submit(str(source))
        assert _wait(job) == "error"
        assert "file_max_s" in (job.error or "")
    finally:
        queue.shutdown()


def test_http_openai_model_alias_and_actual_model_metadata(server):
    httpd, _service, cfg = server
    body, ctype = _multipart({
        "file": ("clip.wav", _wav_bytes()),
        "model": ("", b"whisper-1"),
        "response_format": ("", b"verbose_json"),
    })
    status, payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                               body=body, headers={"Content-Type": ctype})
    assert status == 200, payload
    assert json.loads(payload)["model"] == cfg["model"]

    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes()),
                              "model": ("", b"not-installed")})
    status, payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                               body=body, headers={"Content-Type": ctype})
    assert status == 400
    assert b"is not installed" in payload


def test_http_jobs_model_contract_and_metadata(server, tmp_path):
    httpd, _service, cfg = server
    source = tmp_path / "clip.wav"
    source.write_bytes(_wav_bytes(0.2))
    payload = json.dumps({"path": str(source), "model": "whisper-1"}).encode()
    status, body, _ = _http(httpd, "POST", "/jobs", body=payload,
                            headers={"Content-Type": "application/json"})
    assert status == 202
    job_id = json.loads(body)["job_id"]
    status, body, _ = _http(httpd, "GET", "/jobs/" + job_id)
    assert status == 200
    assert json.loads(body)["model"] == cfg["model"]

    payload = json.dumps({"path": str(source), "model": "gpt-9"}).encode()
    status, body, _ = _http(httpd, "POST", "/jobs", body=payload,
                            headers={"Content-Type": "application/json"})
    assert status == 400
    assert b"is not installed" in body


def test_http_openai_passes_prompt_and_temperature(server):
    httpd, service, _cfg = server
    body, ctype = _multipart({
        "file": ("clip.wav", _wav_bytes()),
        "prompt": ("", b"names: Forma"),
        "temperature": ("", b"0.2"),
    })
    status, payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                               body=body, headers={"Content-Type": ctype})
    assert status == 200, payload
    call = service.asr.calls[-1]
    assert call["initial_prompt"] == "names: Forma"
    assert call["temperature"] == 0.2

    for name, value in (("temperature", b"abc"), ("temperature", b"2.0"),
                        ("prompt", b"x" * 1025)):
        body, ctype = _multipart({"file": ("clip.wav", _wav_bytes()),
                                  name: ("", value)})
        status, _payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                                    body=body, headers={"Content-Type": ctype})
        assert status == 400, (name, value)


def test_engine_default_prompt_and_explicit_override():
    engine = _fake_asr()
    list(engine.transcribe_array(np.zeros((16000,), dtype=np.float32), 1.0))
    _audio, kwargs = engine.model.calls[0]
    assert kwargs["initial_prompt"]
    assert "temperature" not in kwargs

    engine.model = _FakeModel()
    list(engine.transcribe_array(np.zeros((16000,), dtype=np.float32), 1.0,
                                 initial_prompt="hint", temperature=0.1))
    _audio, kwargs = engine.model.calls[0]
    assert kwargs["initial_prompt"] == "hint"
    assert kwargs["temperature"] == 0.1


def test_clip_timestamps_limit_the_audio_and_never_pass_the_file_end(monkeypatch):
    import asr

    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    seen = []

    class ClipModel:
        def transcribe(self, audio, **kwargs):
            seen.append((audio.shape[0], kwargs))

            def rows():
                yield _Row(Segment(0.0, audio.shape[0] / 16000.0, "block"))

            return rows(), _Row(None, language="ru")

    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, FakeLog())
    engine.model = ClipModel()
    audio = np.zeros((10 * 16000,), dtype=np.float32)

    segments = list(engine.transcribe_array(
        audio, 10.0, clip_timestamps="2,4.5", initial_prompt="names: Forma"))

    assert [samples for samples, _kwargs in seen] == [16000, 16000, 8000]
    assert seen[0][1]["initial_prompt"] == "names: Forma"
    assert [(round(s.start, 2), round(s.end, 2)) for s in segments] == [
        (2.0, 3.0), (3.0, 4.0), (4.0, 4.5)]

    seen.clear()
    past_end = list(engine.transcribe_array(
        audio, 10.0, clip_timestamps="8,60"))

    assert [samples for samples, _kwargs in seen] == [16000, 16000]
    assert past_end and max(segment.end for segment in past_end) == 10.0
    assert all(segment.end <= 10.0 for segment in past_end)


def test_clip_timestamps_refuse_a_reversed_range():
    import asr

    with pytest.raises(ValueError):
        asr.parse_clip_timestamps("5,2")
    with pytest.raises(ValueError):
        asr.parse_clip_timestamps("two,5")
    assert asr.parse_clip_timestamps("") == []
    assert asr.parse_clip_timestamps("10", 20.0) == [(10.0, 20.0)]
    assert asr.parse_clip_timestamps("10,40", 20.0) == [(10.0, 20.0)]
    assert asr.format_clip_timestamps([(2.0, 5.0)]) == "2,5"


def test_srt_clock_rounds_one_millisecond_total():
    cases = [(59.9994, "00:00:59,999"), (59.9995, "00:01:00,000"),
             (347.9995, "00:05:48,000"), (3599.9996, "01:00:00,000"),
             (-1.0, "00:00:00,000")]
    for seconds, expected in cases:
        srt = transcript.to_srt([Segment(seconds, seconds, "x")])
        assert srt.splitlines()[1] == f"{expected} --> {expected}"
        vtt_line = expected.replace(",", ".")
        vtt = transcript.to_vtt([Segment(seconds, seconds, "x")])
        assert vtt.splitlines()[2] == f"{vtt_line} --> {vtt_line}"
        for body in (srt, vtt):
            assert ",1000" not in body and ".1000" not in body


def _wait_until_no_temp_files(tmp_path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and list(tmp_path.glob("_file_in_*")):
        time.sleep(0.02)


def test_v1_temp_file_is_deleted_after_success_and_error(server, tmp_path):
    httpd, service, _cfg = server
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
    status, _payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                                body=body, headers={"Content-Type": ctype})
    assert status == 200
    _wait_until_no_temp_files(tmp_path)
    assert not list(tmp_path.glob("_file_in_*"))

    service.asr.raise_error = True
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
    status, _payload, _ = _http(httpd, "POST", "/v1/audio/transcriptions",
                                body=body, headers={"Content-Type": ctype})
    assert status == 500
    _wait_until_no_temp_files(tmp_path)
    assert not list(tmp_path.glob("_file_in_*"))


def test_jobs_multipart_temp_is_deleted_after_success_error_and_refusal(
        server, tmp_path):
    httpd, service, cfg = server
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
    status, _payload, _ = _http(httpd, "POST", "/jobs", body=body,
                                headers={"Content-Type": ctype})
    assert status == 202
    deadline = time.time() + 5
    while time.time() < deadline and list(tmp_path.glob("_file_job_*")):
        time.sleep(0.02)
    assert not list(tmp_path.glob("_file_job_*"))

    service.asr.raise_error = True
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
    status, _payload, _ = _http(httpd, "POST", "/jobs", body=body,
                                headers={"Content-Type": ctype})
    assert status == 202
    deadline = time.time() + 5
    while time.time() < deadline and list(tmp_path.glob("_file_job_*")):
        time.sleep(0.02)
    assert not list(tmp_path.glob("_file_job_*"))

    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes()),
                              "model": ("", b"gpt-9")})
    status, _payload, _ = _http(httpd, "POST", "/jobs", body=body,
                                headers={"Content-Type": ctype})
    assert status == 400
    assert not list(tmp_path.glob("_file_job_*"))

    service.asr.raise_error = False
    service.file_jobs.shutdown()
    service.file_jobs = _jobs(cfg={**cfg, "file_roots": [str(tmp_path / "allowed")]},
                              asr=service.asr)
    body, ctype = _multipart({"file": ("clip.wav", _wav_bytes())})
    status, _payload, _ = _http(httpd, "POST", "/jobs", body=body,
                                headers={"Content-Type": ctype})
    assert status == 403
    assert not list(tmp_path.glob("_file_job_*"))


# -- batched file mode (card-204) --------------------------------------
class _BatchedPipeline:
    """Stand-in for faster_whisper.BatchedInferencePipeline."""

    instances = []

    def __init__(self, model):
        self.model = model
        self.calls = []
        _BatchedPipeline.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        rows = _segments()

        def _rows():
            for row in rows:
                yield _Row(row)

        return _rows(), _Row(None, language="ru", duration=12.0)


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_file_mode_uses_the_batched_pipeline(monkeypatch):
    import asr

    _BatchedPipeline.instances.clear()
    monkeypatch.setattr(asr, "BatchedInferencePipeline", _BatchedPipeline)
    engine = _fake_asr()

    segments = list(engine.transcribe_file(str(TEST_WAV), language="ru",
                                           batched=True, batch_size=6))

    assert len(_BatchedPipeline.instances) == 1
    pipeline = _BatchedPipeline.instances[0]
    assert pipeline.model is engine.model
    assert engine.model.calls == []
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.26, 6.7, "first phrase"), (6.7, 11.66, "second phrase")]

    audio, kwargs = pipeline.calls[0]
    assert audio.shape == (199760,)
    assert kwargs["language"] == "ru"
    assert kwargs["beam_size"] == 5
    assert kwargs["initial_prompt"]
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["no_speech_threshold"] == asr.NO_SPEECH_THRESHOLD
    assert kwargs["log_prob_threshold"] == asr.LOG_PROB_THRESHOLD
    assert kwargs["vad_filter"] is True
    assert kwargs["vad_parameters"] == asr.VAD_PARAMETERS
    assert kwargs["word_timestamps"] is False
    assert kwargs["without_timestamps"] is False
    assert kwargs["batch_size"] == 6
    assert "temperature" not in kwargs


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_file_mode_short_file_stays_sequential(monkeypatch):
    import asr

    class _NeverBuilt:
        def __init__(self, _model):
            raise AssertionError("no batched pipeline for a short file")

    monkeypatch.setattr(asr, "BatchedInferencePipeline", _NeverBuilt)
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": [], "file_batched": True,
                      "file_batch_size": 8}, FakeLog())
    engine.model = _FakeModel()

    segments = list(engine.transcribe_file(str(TEST_WAV)))

    assert [s.text for s in segments] == ["first phrase", "second phrase"]
    assert len(engine.model.calls) == 1
    assert engine._batched is None


def test_dictation_never_uses_the_batched_pipeline(monkeypatch):
    import asr

    class _NeverBuilt:
        def __init__(self, _model):
            raise AssertionError("dictation must not build a batched pipeline")

    class _SpyBatched:
        def __getattr__(self, name):
            raise AssertionError("dictation touched the batched pipeline: " + name)

    monkeypatch.setattr(asr, "BatchedInferencePipeline", _NeverBuilt)
    engine = _fake_asr()
    engine._batched = _SpyBatched()

    text = engine.transcribe(np.zeros((16000,), dtype=np.float32))
    list(engine.transcribe_array(np.zeros((16000,), dtype=np.float32), 1.0,
                                 batched=False))

    assert text == "first phrase second phrase"
    assert len(engine.model.calls) == 2


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_batched_falls_back_when_unavailable(monkeypatch):
    import asr

    monkeypatch.setattr(asr, "BatchedInferencePipeline", None)
    engine = _fake_asr()
    progress = []

    segments = list(engine.transcribe_file(str(TEST_WAV), batched=True,
                                           on_progress=progress.append))

    assert [s.text for s in segments] == ["first phrase", "second phrase"]
    assert len(engine.model.calls) == 1
    assert engine._batch_error
    assert any("using the sequential path" in line
               for _level, line in engine.log.lines)
    assert progress[-1] == 100.0


@pytest.mark.skipif(not TEST_WAV.is_file(), reason="test_ru.wav is missing")
def test_batched_falls_back_when_construction_fails(monkeypatch):
    import asr

    class _Broken:
        def __init__(self, _model):
            raise RuntimeError("driver says no")

    monkeypatch.setattr(asr, "BatchedInferencePipeline", _Broken)
    engine = _fake_asr()

    segments = list(engine.transcribe_file(str(TEST_WAV), batched=True))

    assert [s.text for s in segments] == ["first phrase", "second phrase"]
    assert len(engine.model.calls) == 1
    assert engine._batched is None
    assert "driver says no" in (engine._batch_error or "")
    assert any("using the sequential path" in line
               for _level, line in engine.log.lines)


def test_batched_falls_back_mid_file_without_duplicates(monkeypatch):
    import asr

    class _BreaksMidBlock:
        def __init__(self, model):
            self.model = model

        def transcribe(self, _audio, **_kwargs):
            def rows():
                yield _Row(Segment(0.0, 1.0, " batched"))
                raise RuntimeError("decode failed")

            return rows(), _Row(None, language="ru")

    monkeypatch.setattr(asr, "BatchedInferencePipeline", _BreaksMidBlock)
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, FakeLog())
    engine.model = _FakeModel([
        Segment(0.0, 1.5, "sequential one"),
        Segment(1.5, 2.0, "sequential two"),
        Segment(2.0, 3.0, "sequential three"),
    ])
    audio = np.zeros((int(4.0 * 16000),), dtype=np.float32)
    progress = []

    segments = list(engine.transcribe_array(
        audio, 4.0, batched=True, on_progress=progress.append))

    texts = [s.text for s in segments]
    assert texts == ["batched", "sequential one", "sequential two",
                     "sequential three"]
    assert [(round(s.start, 2), round(s.end, 2)) for s in segments] == [
        (0.0, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0)]
    starts = [s.start for s in segments]
    assert starts == sorted(starts)
    assert len(engine.model.calls) == 1
    assert progress == sorted(progress)
    assert progress[-1] == 100.0
    warnings = [line for level, line in engine.log.lines
                if "falling back to the sequential path" in line]
    assert len(warnings) == 1


def test_batched_pipeline_gets_one_block_at_a_time(monkeypatch):
    import asr

    sizes = []

    class _Recording:
        def __init__(self, model):
            self.model = model

        def transcribe(self, audio, **_kwargs):
            sizes.append(audio.shape[0])

            def rows():
                yield _Row(Segment(0.0, audio.shape[0] / 16000.0, "block"))

            return rows(), _Row(None, language="ru")

    monkeypatch.setattr(asr, "FILE_CHUNK_S", 1.0)
    monkeypatch.setattr(asr, "BatchedInferencePipeline", _Recording)
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, FakeLog())
    engine.model = _FakeModel()
    audio = np.zeros((int(2.5 * 16000),), dtype=np.float32)

    segments = list(engine.transcribe_array(audio, 2.5, batched=True))

    assert sizes == [16000, 16000, 8000]
    assert [round(s.start, 2) for s in segments] == [0.0, 1.0, 2.0]


def test_batched_keeps_progress_pause_and_cancel(monkeypatch):
    import threading

    import asr

    class _TwoSegments:
        def __init__(self, model):
            self.model = model

        def transcribe(self, _audio, **_kwargs):
            def rows():
                yield _Row(Segment(0.0, 0.5, "one"))
                yield _Row(Segment(0.5, 2.0, "two"))

            return rows(), _Row(None, language="ru")

    monkeypatch.setattr(asr, "BatchedInferencePipeline", _TwoSegments)
    engine = asr.ASR({"language": "ru", "model": "fake", "hotwords": "",
                      "dictionary": []}, FakeLog())
    engine.model = _FakeModel()
    audio = np.zeros((int(2.5 * 16000),), dtype=np.float32)
    progress = []
    paused = {"on": True}
    threading.Timer(0.3, lambda: paused.update(on=False)).start()

    started = time.monotonic()
    segments = list(engine.transcribe_array(
        audio, 2.5, batched=True, on_progress=progress.append,
        should_pause=lambda: paused["on"]))
    waited = time.monotonic() - started

    assert [s.text for s in segments] == ["one", "two"]
    assert progress == sorted(progress)
    assert progress[-1] == 100.0
    assert waited >= 0.25

    cancelled = engine.transcribe_array(audio, 2.5, batched=True,
                                        should_stop=lambda: True)
    with pytest.raises(asr.AsrCancelled):
        next(cancelled)


def test_batched_config_defaults(tmp_path, monkeypatch):
    import config

    assert config.DEFAULTS["file_batched"] is True
    assert config.DEFAULTS["file_batch_size"] == 8

    target = tmp_path / "config.json"
    target.write_text(json.dumps({"model": "large-v3-turbo"}),
                      encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(target))
    loaded = config.load()
    assert loaded["file_batched"] is True
    assert loaded["file_batch_size"] == 8
