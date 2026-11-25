"""HTTP API guards: loopback, token, content type, size, rate, temp file."""
import http.client
import json
import socket
import sys
import time
import types

import main
import pytest

TOKEN = "test-token-123"  # noqa: S105 - a test value, not a secret


class FakeASR:
    def transcribe(self, _audio):
        return "hello"


class FakeService:
    def __init__(self):
        self.state = main.STATE_IDLE
        self.asr = FakeASR()
        self.states = []

    def _set_state(self, state):
        self.state = state
        self.states.append(state)


class FakeLog:
    def debug(self, *args):
        pass

    def info(self, *args):
        pass

    def warning(self, *args):
        pass

    def exception(self, *args):
        pass


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def server(tmp_path, monkeypatch, service):
    fake_audio = types.ModuleType("audio")
    fake_audio.load_wav_16k = lambda _path: object()
    monkeypatch.setitem(sys.modules, "audio", fake_audio)
    monkeypatch.setattr(main, "LOG_DIR", str(tmp_path))
    import vibeforma.http_api as http_api
    monkeypatch.setattr(http_api, "LOG_DIR", str(tmp_path))

    httpd = main.start_http_api(
        {"http_port": 0, "http_token": TOKEN}, service, FakeLog())
    assert httpd is not None
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _post(httpd, path="/transcribe", body=b"RIFF", token=TOKEN,
          content_type="audio/wav", length=None):
    port = httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": content_type}
    if length is None:
        headers["Content-Length"] = str(len(body))
    else:
        headers["Content-Length"] = str(length)
    if token is not None:
        headers[main.HTTP_TOKEN_HEADER] = token
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _get(httpd, path="/health", token=TOKEN):
    port = httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if token is not None:
        headers[main.HTTP_TOKEN_HEADER] = token
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_listens_on_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


def test_health_identifies_the_app_and_actual_port(server):
    status, body = _get(server)

    assert status == 200
    health = json.loads(body)
    assert health["ok"] is True
    assert health["app"] == "vibeforma"
    assert health["api"] == 1
    assert health["port"] == server.server_address[1]


def test_api_falls_back_to_a_free_port_when_the_preferred_one_is_busy(service):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    httpd = None
    try:
        httpd = main.start_http_api(
            {"http_port": busy, "http_token": TOKEN}, service, FakeLog())
        assert httpd is not None
        assert httpd.server_address[1] != busy
        status, body = _get(httpd)
        assert status == 200
        assert json.loads(body)["port"] == httpd.server_address[1]
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        blocker.close()


def test_location_file_names_the_config_and_actual_port(tmp_path, monkeypatch):
    import vibeforma.http_api as http_api

    target = tmp_path / "VibeForma" / "location.json"
    monkeypatch.setattr(http_api, "location_path", lambda: str(target))
    monkeypatch.setattr(http_api, "CONFIG_PATH", str(tmp_path / "config.json"))

    http_api.publish_location(None, port=49321, api="on")

    location = json.loads(target.read_text(encoding="utf-8"))
    assert location["schema"] == 2
    assert location["app"] == "vibeforma"
    assert location["version"]
    assert location["config"] == str(tmp_path / "config.json")
    assert location["port"] == 49321
    assert location["api"] == "on"
    assert "http_token" not in location
    assert "token" not in location


def test_transcribe_returns_json(server):
    status, body = _post(server)

    assert status == 200
    assert json.loads(body)["text"] == "hello"


def test_unknown_path_is_rejected(server):
    status, _ = _post(server, path="/other")

    assert status == 404


def test_busy_service_is_rejected(server, service):
    service.state = main.STATE_RECORDING

    status, _ = _post(server)

    assert status == 409


def test_missing_token_is_rejected(server):
    status, _ = _post(server, token=None)

    assert status == 401


def test_wrong_token_is_rejected(server):
    status, _ = _post(server, token="wrong")  # noqa: S106 - a test value

    assert status == 401


def test_wrong_content_type_is_rejected(server):
    status, _ = _post(server, content_type="application/octet-stream")

    assert status == 415


def test_missing_length_is_rejected(server):
    status, _ = _post(server, body=b"", length=0)

    assert status == 411


def test_oversized_body_is_rejected_without_reading(server):
    status, _ = _post(server, body=b"", length=main.HTTP_MAX_BODY + 1)

    assert status == 413


def test_rate_limit(server):
    first, _ = _post(server)
    second, _ = _post(server)

    assert first == 200
    assert second == 429


def test_temp_wav_is_deleted(server, tmp_path, service):
    status, _ = _post(server)

    assert status == 200
    expected = [main.STATE_PROCESSING, main.STATE_IDLE]
    deadline = time.time() + 2
    # the handler deletes the file and goes idle in finally, after the reply
    while (list(tmp_path.glob("*.wav")) or service.states != expected) \
            and time.time() < deadline:
        time.sleep(0.02)
    assert list(tmp_path.glob("*.wav")) == []
    assert service.states == expected


def test_api_mints_and_saves_a_token_when_missing(service, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg = {"http_port": 0}

    httpd = main.start_http_api(cfg, service, FakeLog())
    try:
        assert httpd is not None
        assert cfg["http_token"]
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["http_token"] == cfg["http_token"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- the agent contract: prompt, words and the clip range -----------------
BOUNDARY = "----vibeforma-test"


def _multipart(fields):
    body = b""
    for name, (filename, payload) in fields.items():
        body += f"--{BOUNDARY}\r\n".encode()
        if filename:
            body += (f'Content-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\n\r\n').encode()
        else:
            body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     ).encode()
        body += payload + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return body, f"multipart/form-data; boundary={BOUNDARY}"


class FakeQueue:
    """A job queue that records the contract and reports one job back."""

    def __init__(self):
        self.submitted = None
        self.job = types.SimpleNamespace(
            id="job-1", state="queued", progress=0.0, source="",
            language="", engine_model="fake", duration=0.0, segments=[],
            outputs=[], prompt="", word_timestamps=None,
            clip_timestamps="", error=None)

    def submit(self, source, **kwargs):
        self.submitted = {"source": source, **kwargs}
        self.job.source = source
        self.job.prompt = kwargs.get("prompt", "")
        self.job.word_timestamps = kwargs.get("word_timestamps")
        self.job.clip_timestamps = kwargs.get("clip_timestamps", "")
        return self.job

    def get(self, job_id):
        return self.job if job_id == self.job.id else None


def test_jobs_accept_prompt_words_and_clip(server, service):
    queue = FakeQueue()
    service.file_jobs = queue
    body, ctype = _multipart({
        "file": ("clip.wav", b"RIFF"),
        "prompt": ("", b"names: Forma"),
        "word_timestamps": ("", b"true"),
        "clip_timestamps": ("", b"2,5"),
    })

    status, payload = _post(server, path="/jobs", body=body,
                            content_type=ctype)

    assert status == 202, payload
    assert queue.submitted["prompt"] == "names: Forma"
    assert queue.submitted["word_timestamps"] is True
    assert queue.submitted["clip_timestamps"] == "2,5"
    status, payload = _get(server, path="/jobs/job-1")
    job = json.loads(payload)
    assert job["prompt"] == "names: Forma"
    assert job["word_timestamps"] is True
    assert job["clip_timestamps"] == "2,5"


def test_jobs_json_contract_and_refusals(server, service):
    queue = FakeQueue()
    service.file_jobs = queue
    request = {"path": r"D:\audio\a.wav", "prompt": "hint",
               "word_timestamps": True, "clip_timestamps": [2, 5]}

    status, payload = _post(server, path="/jobs",
                            body=json.dumps(request).encode(),
                            content_type="application/json")

    assert status == 202, payload
    assert queue.submitted["prompt"] == "hint"
    assert queue.submitted["word_timestamps"] is True
    assert queue.submitted["clip_timestamps"] == "2,5"

    for bad in ({"clip_timestamps": "5,2"}, {"clip_timestamps": "two,5"},
                {"prompt": "x" * 1025}):
        status, payload = _post(server, path="/jobs",
                                body=json.dumps({**request, **bad}).encode(),
                                content_type="application/json")
        assert status == 400, (bad, payload)


class VerboseASR(FakeASR):
    """One segment with the engine readings and one word with probability."""

    def __init__(self):
        self.calls = []

    def transcribe_array(self, _audio, _duration, **kwargs):
        from transcript import Segment, Word
        self.calls.append(kwargs)
        return [Segment(0.0, 1.0, " hello",
                        words=[Word(0.0, 0.5, " hello", 0.75)],
                        avg_logprob=-0.3, compression_ratio=1.2,
                        no_speech_prob=0.05, temperature=0.0)]


def test_v1_verbose_json_matches_the_openai_schema(server, service,
                                                   monkeypatch):
    import asr
    monkeypatch.setattr(asr, "decode_audio_file",
                        lambda *_args, **_kwargs: (object(), 1.0))
    engine = VerboseASR()
    service.asr = engine
    service.file_jobs = object()
    body, ctype = _multipart({
        "file": ("clip.wav", b"RIFF"),
        "response_format": ("", b"verbose_json"),
        "timestamp_granularities[]": ("", b"word"),
        "clip_timestamps": ("", b"0,1"),
    })

    status, payload = _post(server, path="/v1/audio/transcriptions", body=body,
                            content_type=ctype)

    assert status == 200, payload
    answer = json.loads(payload)
    segment = answer["segments"][0]
    assert segment["avg_logprob"] == -0.3
    assert segment["no_speech_prob"] == 0.05
    assert segment["compression_ratio"] == 1.2
    assert segment["temperature"] == 0.0
    assert answer["words"] == [{"word": " hello", "start": 0.0, "end": 0.5,
                                "probability": 0.75}]
    assert engine.calls[0]["word_timestamps"] is True
    assert engine.calls[0]["clip_timestamps"] == "0,1"
    # No prompt in the request: the engine keeps the configured preset.
    assert engine.calls[0]["initial_prompt"] is None
