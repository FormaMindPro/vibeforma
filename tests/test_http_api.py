"""HTTP API guards: loopback, token, content type, size, rate, temp file."""
import http.client
import json
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

    httpd = main.start_http_api(
        {"http_port": 0, "http_token": TOKEN}, service, FakeLog())
    assert httpd is not None
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _post(httpd, path="/transcribe", body=b"RIFF", token=TOKEN,  # noqa: S106
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


def test_listens_on_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


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
    deadline = time.time() + 2
    while list(tmp_path.glob("*.wav")) and time.time() < deadline:
        time.sleep(0.02)   # the handler deletes it in finally, after the reply
    assert list(tmp_path.glob("*.wav")) == []
    assert service.states == [main.STATE_PROCESSING, main.STATE_IDLE]


def test_api_refuses_to_start_without_a_token(service):
    httpd = main.start_http_api({"http_port": 0}, service, FakeLog())

    assert httpd is None
