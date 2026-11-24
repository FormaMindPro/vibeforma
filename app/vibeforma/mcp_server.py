"""MCP server over stdio: the local HTTP API as tools for an AI agent.

The server speaks newline-delimited JSON-RPC 2.0, the MCP stdio transport,
and writes protocol messages to stdout only. It finds the running service
through location.json in %LOCALAPPDATA%\\VibeForma and reads the token from
the config file that location.json names. No network listener is opened:
the service stays loopback-only.
"""
from __future__ import annotations

import io
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, NamedTuple, TextIO

from config import VERSION

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "vibeforma"
TOKEN_HEADER = "X-VibeForma-Token"  # noqa: S105 - a header name
DEFAULT_TIMEOUT_S = 3600
POLL_INTERVAL_S = 1.0
FILE_FORMATS = ("txt", "json", "srt", "vtt")
ENDED_STATES = ("error", "cancelled", "skipped")
ALLOWED_SCHEME = "http"
ALLOWED_HOST = "127.0.0.1"


class ServiceError(RuntimeError):
    """The local service cannot serve a request."""


class Service(NamedTuple):
    """Where the running service listens and which token it expects."""

    base_url: str
    token: str


TOOLS: list[dict[str, Any]] = [
    {
        "name": "vibeforma_capabilities",
        "description": (
            "Report the local VibeForma transcriber: models, languages, "
            "output formats and limits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "vibeforma_transcribe_file",
        "description": (
            "Transcribe one local audio or video file through the running "
            "VibeForma service and return the text. Long files are queued "
            "and polled until the job finishes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to a local media file.",
                },
                "language": {
                    "type": "string",
                    "description": "Language code; empty follows the service.",
                },
                "format": {
                    "type": "string",
                    "enum": list(FILE_FORMATS),
                    "description": "Output format; txt by default.",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "How long to wait for the job, seconds.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "vibeforma_job_status",
        "description": "Read the state of a queued or running transcription job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job id returned by the service.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
]


def default_location_path() -> str:
    """The one path a local agent reads without scanning the machine."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if not base:
        return ""
    return os.path.join(base, "VibeForma", "location.json")


def load_location(path: str | None = None) -> dict[str, Any]:
    """Read location.json; a missing or broken file is a named refusal."""
    target = path or default_location_path()
    if not target:
        raise ServiceError("LOCALAPPDATA is not set; cannot find VibeForma")
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise ServiceError(f"VibeForma location file is missing: {target}") from error
    except ValueError as error:
        raise ServiceError(f"VibeForma location file is broken: {target}") from error
    if not isinstance(data, dict):
        raise ServiceError("VibeForma location file has an unexpected shape")
    return dict(data)


def load_service(path: str | None = None) -> Service:
    """Read the endpoint and the token of the running service."""
    location = load_location(path)
    api = str(location.get("api") or "")
    if api != "on":
        raise ServiceError("the local API is off; enable it in Settings")
    try:
        port = int(location.get("port") or 0)
    except (TypeError, ValueError) as error:
        raise ServiceError("location.json carries no port") from error
    if port <= 0:
        raise ServiceError("location.json carries no port")
    config_path = str(location.get("config") or "")
    if not config_path:
        raise ServiceError("location.json names no config file")
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except OSError as error:
        raise ServiceError(f"VibeForma config is unreadable: {config_path}") from error
    except ValueError as error:
        raise ServiceError(f"VibeForma config is broken: {config_path}") from error
    token = str(config.get("http_token") or "") if isinstance(config, dict) else ""
    if not token:
        raise ServiceError("the API token is empty; restart VibeForma with the API on")
    return Service(f"http://127.0.0.1:{port}", token)


def validated_url(service: Service, path: str) -> str:
    """Pin the request to this machine's loopback endpoint, nothing else.

    location.json is a local file, but the server still refuses anything that
    is not plain http on the literal loopback address: a tampered file can
    change the port, never the destination class.
    """
    parsed = urllib.parse.urlsplit(service.base_url)
    if parsed.scheme != ALLOWED_SCHEME:
        raise ServiceError("only http is allowed")
    if parsed.username or parsed.password:
        raise ServiceError("credentials in the URL are not allowed")
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ServiceError("the endpoint host must be a literal IP") from error
    if not address.is_loopback or str(address) != ALLOWED_HOST:
        raise ServiceError("only the loopback endpoint is allowed")
    try:
        port = parsed.port
    except ValueError as error:
        raise ServiceError("the endpoint port is invalid") from error
    if port is None or not 1 <= port <= 65535:
        raise ServiceError("the endpoint port is invalid")
    if not path.startswith("/") or path.startswith("//"):
        raise ServiceError("the request path must be a single absolute path")
    return f"{ALLOWED_SCHEME}://{ALLOWED_HOST}:{port}{path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the service never sends one, a tampered one is not
    followed to another host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        raise ServiceError("redirects are not allowed")


def api_request(service: Service, method: str, path: str,
                payload: dict[str, Any] | None = None,
                timeout: float = 30.0) -> tuple[int, bytes]:
    """One request to the validated loopback endpoint; status is returned."""
    data = None
    headers = {TOKEN_HEADER: service.token}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310 - the URL passed validated_url
        validated_url(service, path), data=data,
        headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()
    except (urllib.error.URLError, OSError) as error:
        raise ServiceError(f"VibeForma does not answer: {error}") from error


def api_json(service: Service, method: str, path: str,
             payload: dict[str, Any] | None = None,
             timeout: float = 30.0) -> dict[str, Any]:
    """A request whose body must be a JSON object with a 2xx status."""
    status, body = api_request(service, method, path, payload, timeout)
    if not 200 <= status < 300:
        detail = body[:200].decode("utf-8", "replace")
        raise ServiceError(f"VibeForma answered {status}: {detail}")
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as error:
        raise ServiceError("VibeForma returned invalid JSON") from error
    if not isinstance(data, dict):
        return {"value": data}
    return data


def tool_capabilities(service: Service) -> dict[str, Any]:
    """GET /capabilities: what the service can do right now."""
    return api_json(service, "GET", "/capabilities")


def tool_transcribe(service: Service, path: str, language: str = "",
                    fmt: str = "txt",
                    timeout_s: float = DEFAULT_TIMEOUT_S,
                    poll_s: float = POLL_INTERVAL_S) -> dict[str, Any]:
    """Queue a file, wait for the job and return its rendered text."""
    if fmt not in FILE_FORMATS:
        raise ServiceError("format must be txt, json, srt or vtt")
    created = api_json(service, "POST", "/jobs",
                       {"path": path, "language": language, "formats": [fmt]})
    job_id = str(created.get("job_id") or "")
    if not job_id:
        raise ServiceError("VibeForma did not return a job id")
    quoted = urllib.parse.quote(job_id, safe="")
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    status = created
    while time.monotonic() < deadline:
        status = api_json(service, "GET", "/jobs/" + quoted)
        state = str(status.get("state") or "")
        if state == "done":
            break
        if state in ENDED_STATES:
            raise ServiceError(
                f"job {job_id} ended as {state}: {status.get('error') or ''}")
        time.sleep(poll_s)
    else:
        raise ServiceError(f"job {job_id} did not finish in {int(timeout_s)} s")
    query = urllib.parse.urlencode({"format": fmt})
    result_status, result_body = api_request(
        service, "GET", f"/jobs/{quoted}/result?{query}", timeout=30.0)
    if result_status != 200:
        raise ServiceError(f"VibeForma answered {result_status} for the job result")
    return {
        "job_id": job_id,
        "state": "done",
        "format": fmt,
        "text": result_body.decode("utf-8", "replace"),
    }


def tool_job_status(service: Service, job_id: str) -> dict[str, Any]:
    """GET /jobs/<id>: the live state of one job."""
    quoted = urllib.parse.quote(job_id, safe="")
    return api_json(service, "GET", "/jobs/" + quoted)


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


class McpServer:
    """MCP methods over the local service; transport-agnostic for tests."""

    def __init__(self, loader: Callable[[], Service] = load_service) -> None:
        self._loader = loader

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message in, one response out; notifications - None."""
        method = str(message.get("method") or "")
        message_id = message.get("id")
        notification = "id" not in message
        try:
            if method == "initialize":
                return _result(message_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                })
            if method == "ping":
                return _result(message_id, {})
            if method == "tools/list":
                return _result(message_id, {"tools": TOOLS})
            if method == "tools/call":
                return _result(message_id, self._call_tool(message.get("params")))
            if notification or method.startswith("notifications/"):
                return None
            return _error(message_id, -32601, f"unknown method: {method}")
        except ServiceError as error:
            if notification:
                return None
            return _error(message_id, -32000, str(error))
        except Exception as error:  # noqa: BLE001 - a server answers, never dies
            if notification:
                return None
            return _error(message_id, -32603, f"internal error: {error}")

    def _call_tool(self, params: Any) -> dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "vibeforma_capabilities":
                result = tool_capabilities(self._loader())
            elif name == "vibeforma_transcribe_file":
                path = str(args.get("path") or "").strip()
                if not path:
                    raise ServiceError("path is required")
                result = tool_transcribe(
                    self._loader(),
                    path,
                    language=str(args.get("language") or ""),
                    fmt=str(args.get("format") or "txt"),
                    timeout_s=float(args.get("timeout_s") or DEFAULT_TIMEOUT_S),
                )
            elif name == "vibeforma_job_status":
                job_id = str(args.get("job_id") or "").strip()
                if not job_id:
                    raise ServiceError("job_id is required")
                result = tool_job_status(self._loader(), job_id)
            else:
                raise ServiceError(f"unknown tool: {name}")
        except (ServiceError, TypeError, ValueError) as error:
            return {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }
        text = json.dumps(result, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": False}


def serve(stdin: TextIO, stdout: TextIO, server: McpServer) -> None:
    """The MCP stdio loop: one JSON message per line."""
    for line in stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError:
            response: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            if isinstance(message, dict):
                response = server.handle(message)
            else:
                response = _error(None, -32600, "invalid request")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    """Run until stdin closes; stdout carries protocol messages only."""
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="\n")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              newline="\n", write_through=True)
    serve(stdin, stdout, McpServer())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
