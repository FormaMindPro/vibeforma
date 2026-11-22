"""Optional local HTTP API: loopback only, token, audio/wav, 10 MB, 1 req/s."""
from __future__ import annotations

import contextlib
import errno
import hmac
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from config import CONFIG_PATH, DATA_DIR, LOG_DIR, VERSION

from . import STATE_IDLE, STATE_PROCESSING

HTTP_MAX_BODY = 10 * 1000 * 1000   # bytes accepted for one request
HTTP_MIN_INTERVAL_S = 1.0          # one request per second
HTTP_TOKEN_HEADER = "X-VibeForma-Token"  # noqa: S105 - a header name


class _LoopbackServer(ThreadingHTTPServer):
    """A loopback listener that reports a busy port instead of sharing it.

    Windows SO_REUSEADDR lets a second socket bind a port that is already
    listening, and the first listener keeps receiving the traffic. Disabling
    it makes EADDRINUSE real, which is what the free-port fallback needs.
    """
    allow_reuse_address = sys.platform != "win32"


def location_path() -> str:
    """Where a form looks for this install: LOCALAPPDATA is the one place a
    browser-side tool can find without knowing the install folder.

    The environment value is resolved and the result must stay under it: a
    path outside its own base is refused, not repaired.
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if not base:
        return ""
    try:
        root = Path(base).resolve()
        target = (root / "VibeForma" / "location.json").resolve()
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return ""
    return str(target)


def publish_location(log: Any = None, *, port: int | None = None,
                     api: str | None = None) -> None:
    """Write the config path and the actual API endpoint for external tools.

    Carries no secret: the token stays in config.json alone. A source install
    is found through this file exactly like a packed one. ``port`` is the port
    the listener actually bound (a busy preferred port falls back to a free
    one), and ``api`` is ``on``, ``off`` or ``error``.
    """
    target = location_path()
    if not target:
        return
    payload: dict[str, Any] = {
        "schema": 1,
        "app": "vibeforma",
        "version": VERSION,
        "config": CONFIG_PATH,
        "app_dir": DATA_DIR,
        "pid": os.getpid(),
        "updated": int(time.time()),
    }
    if port is not None:
        payload["port"] = int(port)
    if api is not None:
        payload["api"] = str(api)
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        if log:
            log.debug("location.json not written: %s", e)


def _is_addr_in_use(error: OSError) -> bool:
    """Windows reports WSAEADDRINUSE (10048); POSIX reports errno.EADDRINUSE."""
    return getattr(error, "winerror", None) == 10048 or error.errno in (
        errno.EADDRINUSE, 10048)


def start_http_api(cfg: dict[str, Any], svc: Any,
                   log: Any) -> ThreadingHTTPServer | None:
    """GET /health and POST /transcribe on 127.0.0.1:<http_port>.

    Off by default. The key is minted and saved on the first start when it is
    missing; if it cannot be saved the server refuses to start (fail-closed).
    /health says whether the API is up, which model and whether the service is
    idle; the transcribe call takes only audio/wav up to 10 MB, one request per
    second, works only while the service is idle, and deletes the temporary WAV.
    """
    token = str(cfg.get("http_token") or "").strip()
    if not token:
        # The switch can be on in a config written by hand or by an older build:
        # the key is minted here and saved once, so the form can read it.
        import secrets

        from config import save
        token = secrets.token_urlsafe(24)
        cfg["http_token"] = token
        try:
            save(cfg)
            log.info("HTTP API: token generated and saved")
        except OSError as e:
            cfg.pop("http_token", None)
            log.warning("HTTP API: token could not be saved (%s) — refusing "
                        "to start", e)
            return None
    last_call = [0.0]

    def set_state(state: str) -> None:
        setter = getattr(svc, "_set_state", None)
        if setter:
            setter(state)
        else:
            svc.state = state

    class Handler(BaseHTTPRequestHandler):
        timeout = 15

        def _authorized(self) -> bool:
            given = self.headers.get(HTTP_TOKEN_HEADER, "")
            return hmac.compare_digest(given, token)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            """GET /health - is the API up, which model, is the service idle.

            A form probes this before it offers its microphone. The token is
            required here too: availability is not a public fact.
            """
            if self.path != "/health":
                self._reject(404)
                return
            if not self._authorized():
                self._reject(401, "missing or wrong X-VibeForma-Token")
                return
            self._json(200, {
                "ok": True,
                "app": "vibeforma",
                "api": 1,
                "port": int(self.server.server_address[1]),
                "version": VERSION,
                "model": str(cfg.get("model") or ""),
                "language": str(cfg.get("language") or ""),
                "state": str(svc.state),
            })

        def _reject(self, code: int, message: str | None = None) -> None:
            """Answer without the unread body: drain a small one first, so the
            client reads the response instead of a connection reset."""
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if 0 < length <= 1024 * 1024:
                # Draining is best effort: a broken stream is answered anyway.
                with contextlib.suppress(OSError):
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
            self.send_error(code, message)

        def do_POST(self) -> None:
            if self.path != "/transcribe":
                self._reject(404)
                return
            if not self._authorized():
                self._reject(401, "missing or wrong X-VibeForma-Token")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip().lower() != "audio/wav":
                self._reject(415, "Content-Type must be audio/wav")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length <= 0:
                self._reject(411)
                return
            if length > HTTP_MAX_BODY:
                self._reject(413, "body is larger than 10 MB")
                return
            now = time.monotonic()
            if now - last_call[0] < HTTP_MIN_INTERVAL_S:
                self._reject(429, "one request per second")
                return
            last_call[0] = now
            if svc.state != STATE_IDLE:
                self._reject(409)
                return
            tmp = ""
            try:
                data = self.rfile.read(length)
                fd, tmp = tempfile.mkstemp(prefix="_http_in_", suffix=".wav",
                                           dir=LOG_DIR)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                set_state(STATE_PROCESSING)
                import audio as audio_mod
                audio = audio_mod.load_wav_16k(tmp)
                text = svc.asr.transcribe(audio)
                self._json(200, {"text": text})
            except Exception as e:  # report, never crash the server
                log.exception("HTTP API error: %s", e)
                self.send_error(500)
            finally:
                if tmp:
                    with contextlib.suppress(OSError):
                        os.remove(tmp)
                set_state(STATE_IDLE)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("HTTP: " + fmt % args)

    raw_port = cfg.get("http_port")
    try:
        preferred = 8787 if raw_port in (None, "") else int(raw_port)
    except (TypeError, ValueError):
        preferred = 8787
    try:
        server = _LoopbackServer(("127.0.0.1", preferred), Handler)
    except OSError as e:
        if not _is_addr_in_use(e):
            log.warning("HTTP API not started: %s", e)
            return None
        # A fixed default port collides with another local tool on a user's
        # machine: the form still needs an endpoint, so bind a free one and
        # publish the actual port in location.json.
        log.warning("HTTP API: port %d is busy, choosing a free port",
                    preferred)
        try:
            server = _LoopbackServer(("127.0.0.1", 0), Handler)
        except OSError as fallback_error:
            log.warning("HTTP API not started: %s", fallback_error)
            return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    log.info("HTTP API: http://127.0.0.1:%d/transcribe (token required)",
             port)
    return server
