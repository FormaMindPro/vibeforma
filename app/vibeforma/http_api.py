"""Optional local HTTP API: loopback only, token, audio/wav, 10 MB, 1 req/s."""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from config import LOG_DIR

from . import STATE_IDLE, STATE_PROCESSING

HTTP_MAX_BODY = 10 * 1000 * 1000   # bytes accepted for one request
HTTP_MIN_INTERVAL_S = 1.0          # one request per second
HTTP_TOKEN_HEADER = "X-VibeForma-Token"  # noqa: S105 - a header name


def start_http_api(cfg: dict[str, Any], svc: Any,
                   log: Any) -> ThreadingHTTPServer | None:
    """POST http://127.0.0.1:<http_port>/transcribe with a WAV body.

    Off by default. Needs http_token (fail-closed without it), takes only
    audio/wav up to 10 MB, one request per second, works only while the
    service is idle, and always deletes the temporary WAV.
    """
    token = str(cfg.get("http_token") or "").strip()
    if not token:
        log.warning("HTTP API is enabled but http_token is empty — refusing "
                    "to start (set http_token in config.json)")
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
            given = self.headers.get(HTTP_TOKEN_HEADER, "")
            if not hmac.compare_digest(given, token):
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
                body = json.dumps({"text": text},
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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

    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(cfg["http_port"])),
                                     Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("HTTP API: http://127.0.0.1:%d/transcribe (token required)",
                 cfg["http_port"])
        return server
    except OSError as e:
        log.warning("HTTP API not started: %s", e)
        return None
