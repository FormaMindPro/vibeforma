"""MCP server: handshake, tools, loopback-only requests and job polling."""
from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from vibeforma import mcp_server

TOKEN = "test-token"  # noqa: S105 - a test value, not a secret


class StubService(BaseHTTPRequestHandler):
    """A loopback stand-in for the product's HTTP API."""

    posted: dict = {}

    def _send(self, code: int, body: str,
              content_type: str = "application/json") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return self.headers.get("X-VibeForma-Token") == TOKEN

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(401, '{"ok": false}')
            return
        if self.path == "/capabilities":
            self._send(200, json.dumps(
                {"formats": ["txt"], "model": "test-model"}))
            return
        if self.path == "/jobs/j1":
            self._send(200, json.dumps({
                "id": "j1", "state": "done", "prompt": "names: Forma",
                "word_timestamps": True, "clip_timestamps": "2,5"}))
            return
        if self.path.startswith("/jobs/j1/result"):
            self._send(200, "hello world", "text/plain; charset=utf-8")
            return
        self._send(404, '{"ok": false}')

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, '{"ok": false}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        assert str(payload.get("path") or "").endswith("a.wav")
        StubService.posted = payload
        self._send(202, json.dumps({"job_id": "j1", "state": "queued"}))

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def service(stub):
    port = int(stub.server_address[1])
    return mcp_server.Service(f"http://127.0.0.1:{port}", TOKEN)


def _server(service):
    return mcp_server.McpServer(loader=lambda: service)


def test_initialize_reports_protocol_and_tools(service):
    response = _server(service).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "vibeforma"
    assert "tools" in result["capabilities"]


def test_tools_list_names_the_three_tools(service):
    response = _server(service).handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {
        "vibeforma_capabilities",
        "vibeforma_transcribe_file",
        "vibeforma_job_status",
    }


def test_transcribe_tool_offers_the_engine_contract(service):
    response = _server(service).handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    properties = tools["vibeforma_transcribe_file"]["inputSchema"]["properties"]
    assert {"prompt", "word_timestamps", "clip_timestamps"} <= set(properties)


def test_transcribe_tool_sends_prompt_words_and_clip(service):
    StubService.posted = {}
    response = _server(service).handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {
            "name": "vibeforma_transcribe_file",
            "arguments": {"path": r"D:\audio\a.wav", "timeout_s": 5,
                          "prompt": "names: Forma", "word_timestamps": True,
                          "clip_timestamps": "2,5"},
        },
    })
    assert response is not None
    assert response["result"]["isError"] is False
    assert StubService.posted["prompt"] == "names: Forma"
    assert StubService.posted["word_timestamps"] is True
    assert StubService.posted["clip_timestamps"] == "2,5"


def test_job_status_reports_the_applied_contract(service):
    response = _server(service).handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "vibeforma_job_status",
                   "arguments": {"job_id": "j1"}},
    })
    assert response is not None
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["prompt"] == "names: Forma"
    assert payload["word_timestamps"] is True
    assert payload["clip_timestamps"] == "2,5"


def test_capabilities_tool_returns_service_data(service):
    response = _server(service).handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "vibeforma_capabilities", "arguments": {}},
    })
    assert response is not None
    assert response["result"]["isError"] is False
    text = response["result"]["content"][0]["text"]
    assert "test-model" in text


def test_transcribe_tool_polls_and_returns_text(service):
    response = _server(service).handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {
            "name": "vibeforma_transcribe_file",
            "arguments": {"path": r"D:\audio\a.wav", "timeout_s": 5},
        },
    })
    assert response is not None
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["text"] == "hello world"
    assert payload["job_id"] == "j1"


def test_unknown_tool_is_an_error_result(service):
    response = _server(service).handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "no_such_tool", "arguments": {}},
    })
    assert response is not None
    assert response["result"]["isError"] is True


def test_missing_path_is_an_error_result(service):
    response = _server(service).handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "vibeforma_transcribe_file", "arguments": {}},
    })
    assert response is not None
    assert response["result"]["isError"] is True


def test_wrong_token_is_an_error_result(service):
    wrong = mcp_server.Service(service.base_url, "wrong")
    response = _server(wrong).handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "vibeforma_capabilities", "arguments": {}},
    })
    assert response is not None
    assert response["result"]["isError"] is True


def test_notification_gets_no_response(service):
    assert _server(service).handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_a_jsonrpc_error(service):
    response = _server(service).handle(
        {"jsonrpc": "2.0", "id": 8, "method": "resources/list"})
    assert response is not None
    assert response["error"]["code"] == -32601


@pytest.mark.parametrize("base_url", [
    "http://192.168.1.5:8787",
    "http://localhost:8787",
    "https://127.0.0.1:8787",
    "http://127.0.0.1:0",
])
def test_validated_url_refuses_anything_but_loopback(base_url):
    with pytest.raises(mcp_server.ServiceError):
        mcp_server.validated_url(
            mcp_server.Service(base_url, TOKEN), "/capabilities")


def test_validated_url_keeps_loopback():
    url = mcp_server.validated_url(
        mcp_server.Service("http://127.0.0.1:8787", TOKEN), "/capabilities")
    assert url == "http://127.0.0.1:8787/capabilities"


def test_serve_reads_lines_and_writes_responses(service):
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n")
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout, _server(service))
    lines = [line for line in stdout.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["result"] == {}


def test_load_service_reads_location_and_token(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"http_token": TOKEN}), encoding="utf-8")
    location = tmp_path / "location.json"
    location.write_text(json.dumps({
        "api": "on", "port": 9999, "config": str(config),
    }), encoding="utf-8")
    service = mcp_server.load_service(str(location))
    assert service == mcp_server.Service("http://127.0.0.1:9999", TOKEN)


def test_load_service_refuses_api_off(tmp_path):
    location = tmp_path / "location.json"
    location.write_text(json.dumps({"api": "off", "port": 9999}),
                        encoding="utf-8")
    with pytest.raises(mcp_server.ServiceError):
        mcp_server.load_service(str(location))
