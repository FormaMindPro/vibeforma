"""HTTP API guards: loopback only, idle only, /transcribe only. No model."""
import http.client
import json
import sys
import types

import main
import pytest


class FakeASR:
    def transcribe(self, _audio):
        return "hello"


class FakeService:
    def __init__(self):
        self.state = main.STATE_IDLE
        self.asr = FakeASR()


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

    httpd = main.start_http_api({"http_port": 0}, service, FakeLog())
    assert httpd is not None
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _post(httpd, path="/transcribe", body=b"RIFF"):
    port = httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body,
                     headers={"Content-Length": str(len(body))})
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
