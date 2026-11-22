"""Optional local HTTP API: loopback only, token, dictation and file mode.

The dictation endpoint keeps its old contract: audio/wav, 10 MB, one request
per second, idle only. The file mode adds the OpenAI-compatible
``/v1/audio/transcriptions`` for short files and the job API (``/jobs``) for
long ones, plus ``/capabilities`` for a harness that finds no location file.
"""
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
import urllib.parse
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from config import (
    CONFIG_PATH,
    DATA_DIR,
    ENTRY_PATH,
    INSTALL_DIR,
    LANG_NAMES,
    LAUNCHER_PATH,
    LOG_DIR,
    MODELS,
    PYTHON_PATH,
    VERSION,
)

from . import STATE_IDLE, STATE_PROCESSING
from .pipe import pipe_name

HTTP_MAX_BODY = 10 * 1000 * 1000   # bytes accepted for one request
HTTP_MIN_INTERVAL_S = 1.0          # one request per second
HTTP_TOKEN_HEADER = "X-VibeForma-Token"  # noqa: S105 - a header name
FILE_FORMATS = ("txt", "json", "srt", "vtt")
BEARER_PREFIX = "Bearer "   # Authorization: Bearer <http_token>
PROMPT_MAX_CHARS = 1024     # OpenAI-compatible prompt cap for the file mode
FILE_CONTENT_TYPES = {
    "json": "application/json; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "srt": "text/plain; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
}


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
                     api: str | None = None, cfg: dict[str, Any] | None = None,
                     name: str | None = None,
                     pipe_started: bool = False) -> None:
    """Write everything a harness needs to find and call this service.

    Schema 2 publishes the launcher, the venv python, the entry file, the
    named pipe, the formats and the limits. It carries no secret: the pipe is
    protected by its own DACL, and the HTTP token stays in config.json.
    ``port`` is the port the listener actually bound (a busy preferred port
    falls back to a free one), and ``api`` is ``on``, ``off`` or ``error``.
    ``pipe`` and ``file_mode: true`` appear only once the channel is actually
    listening; before that the file says ``file_mode: false`` and names no
    pipe, so a harness never calls a channel that does not exist yet.
    """
    target = location_path()
    if not target:
        return
    cfg = cfg or {}
    payload: dict[str, Any] = {
        "schema": 2,
        "app": "vibeforma",
        "version": VERSION,
        "launcher": LAUNCHER_PATH,
        "python": PYTHON_PATH,
        "entry": ENTRY_PATH,
        "install_dir": INSTALL_DIR,
        "config": CONFIG_PATH,
        "app_dir": DATA_DIR,
        "pid": os.getpid(),
        "updated": int(time.time()),
        "file_mode": bool(pipe_started),
        "formats": list(FILE_FORMATS),
        "languages": list(LANG_NAMES),
        "max_file_mb": int(cfg.get("file_max_mb") or 4096),
        "max_file_s": int(cfg.get("file_max_s") or 6 * 3600),
        "max_sync_s": int(cfg.get("file_max_sync_s") or 900),
    }
    if pipe_started:
        payload["pipe"] = name or pipe_name()
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


def _parse_multipart(body: bytes, content_type: str) -> dict[str, tuple[str, bytes]]:
    """Parse multipart/form-data with the stdlib: field -> (filename, bytes).

    A plain form field has an empty filename. No new dependency and no
    streaming: the body is already capped by the caller.
    """
    header = ("Content-Type: " + content_type + "\r\n"
              "MIME-Version: 1.0\r\n\r\n").encode("utf-8")
    message = BytesParser(policy=policy.HTTP).parsebytes(header + body)
    fields: dict[str, tuple[str, bytes]] = {}
    if not message.is_multipart():
        raise ValueError("not multipart/form-data")
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True) or b""
        if not isinstance(payload, bytes):
            payload = b""
        fields[str(name)] = (str(filename), payload)
    return fields


def _form_text(fields: dict[str, tuple[str, bytes]], key: str,
               default: str = "") -> str:
    item = fields.get(key)
    if item is None:
        return default
    return item[1].decode("utf-8", errors="replace").strip()


def start_http_api(cfg: dict[str, Any], svc: Any, log: Any,
                   ) -> ThreadingHTTPServer | None:
    """GET /health, POST /transcribe, /capabilities, /v1 and /jobs.

    Off by default. The key is minted and saved on the first start when it is
    missing; if it cannot be saved the server refuses to start (fail-closed).
    /health says whether the API is up, which model and whether the service is
    idle; the dictation call takes only audio/wav up to 10 MB, one request per
    second, works only while the service is idle, and deletes the temporary
    WAV. Every route listens on 127.0.0.1 and requires the token.
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

    def max_mb() -> int:
        try:
            return max(1, int(cfg.get("file_max_mb") or 4096))
        except (TypeError, ValueError):
            return 4096

    def max_sync_s() -> float:
        try:
            return max(1.0, float(cfg.get("file_max_sync_s") or 900))
        except (TypeError, ValueError):
            return 900.0

    def jobs_queue() -> Any:
        """The service's real queue; None while the model is still loading.

        A queue is never invented here: one created without an engine turns
        every job into an error and never receives the dictation signal, so
        the endpoints answer 503 until the service has one.
        """
        return getattr(svc, "file_jobs", None)

    def actual_model() -> str:
        """The model that actually runs; a request never changes it."""
        name = str(getattr(getattr(svc, "asr", None), "model_name", "") or "")
        return name or str(cfg.get("model") or "")

    def set_state(state: str) -> None:
        setter = getattr(svc, "_set_state", None)
        if setter:
            setter(state)
        else:
            svc.state = state

    def service_idle() -> bool:
        return str(getattr(svc, "state", "")) == STATE_IDLE

    def job_payload(job: Any) -> dict[str, Any]:
        return {
            "id": job.id,
            "state": job.state,
            "progress": round(float(job.progress), 1),
            "source": job.source,
            "language": job.language,
            "model": job.engine_model or actual_model(),
            "duration_s": round(float(job.duration), 3),
            "segments": len(job.segments),
            "outputs": list(job.outputs),
            "error": job.error,
        }

    class Handler(BaseHTTPRequestHandler):
        timeout = 30

        # -- plumbing ---------------------------------------------------
        def _authorized(self) -> bool:
            given = self.headers.get(HTTP_TOKEN_HEADER, "")
            if not given:
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith(BEARER_PREFIX.lower()):
                    given = auth[len(BEARER_PREFIX):].strip()
            return bool(given) and hmac.compare_digest(given, token)

        def _json(self, code: int, payload: dict[str, Any],
                  headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, body: str, ctype: str,
                  headers: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _not_ready(self) -> None:
            """A defined answer while the model loads: no job is created."""
            self._json(503, {"ok": False, "loading": True,
                             "error": "model is loading"},
                       headers={"Retry-After": "2"})

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

        def _body(self, limit: int) -> bytes | None:
            """The request body, or None after an answered refusal."""
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length <= 0:
                self._reject(411)
                return None
            if length > limit:
                self._reject(413, f"body is larger than {limit} bytes")
                return None
            return self.rfile.read(length)

        # -- GET --------------------------------------------------------
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if route == "/health":
                if not self._authorized():
                    self._reject(401, "missing or wrong token")
                    return
                queue = jobs_queue()
                ready = queue is not None and getattr(svc, "asr", None) is not None
                stats = queue.stats() if ready else {}
                self._json(200, {
                    "ok": True,
                    "app": "vibeforma",
                    "api": 1,
                    "port": int(cast(tuple[str, int],
                                     self.server.server_address)[1]),
                    "version": VERSION,
                    "model": str(cfg.get("model") or ""),
                    "language": str(cfg.get("language") or ""),
                    "state": str(svc.state),
                    "file_mode": True if ready else "loading",
                    "model_loading": not ready,
                    "dictation_priority": True,
                    "jobs_running": int(stats.get("jobs_running", 0)),
                    "jobs_queued": int(stats.get("jobs_queued", 0)),
                })
                return
            if route == "/capabilities":
                if not self._authorized():
                    self._reject(401, "missing or wrong token")
                    return
                queue = jobs_queue()
                if queue is None or getattr(svc, "asr", None) is None:
                    self._not_ready()
                    return
                stats = queue.stats()
                self._json(200, {
                    "app": "vibeforma",
                    "version": VERSION,
                    "file_mode": True,
                    "word_timestamps": True,
                    "formats": list(FILE_FORMATS),
                    "models": list(MODELS),
                    "model": str(cfg.get("model") or ""),
                    "languages": list(LANG_NAMES),
                    "language": str(cfg.get("language") or ""),
                    "max_file_mb": max_mb(),
                    "max_file_s": int(cfg.get("file_max_s") or 6 * 3600),
                    "max_sync_s": max_sync_s(),
                    "dictation_priority": True,
                    "jobs_running": int(stats.get("jobs_running", 0)),
                    "jobs_queued": int(stats.get("jobs_queued", 0)),
                    "pipe": pipe_name(),
                    "launcher": LAUNCHER_PATH,
                    "python": PYTHON_PATH,
                    "entry": ENTRY_PATH,
                })
                return
            if route.startswith("/jobs/"):
                self._get_job(route, query)
                return
            self._reject(404)

        def _get_job(self, route: str, query: dict[str, list[str]]) -> None:
            if not self._authorized():
                self._reject(401, "missing or wrong token")
                return
            parts = [p for p in route.split("/") if p]
            if len(parts) < 2:
                self._reject(404)
                return
            job_id = parts[1]
            result = len(parts) >= 3 and parts[2] == "result"
            queue = jobs_queue()
            if queue is None:
                self._not_ready()
                return
            job = queue.get(job_id)
            if job is None:
                self._reject(404, "job not found")
                return
            if not result:
                self._json(200, job_payload(job))
                return
            fmt = (query.get("format") or ["txt"])[0].strip().lower()
            if fmt not in FILE_FORMATS:
                self._json(400, {"ok": False, "error": "unknown format"})
                return
            if job.state != "done":
                self._json(409, {"ok": False, "state": job.state,
                                 "error": "job is not done"})
                return
            try:
                body = queue.result(job_id, fmt)
            except (KeyError, ValueError) as e:
                self._json(409, {"ok": False, "error": str(e)})
                return
            self._text(200, body, FILE_CONTENT_TYPES[fmt])

        # -- POST -------------------------------------------------------
        def do_POST(self) -> None:
            route = urllib.parse.urlsplit(self.path).path
            if route == "/transcribe":
                self._dictation()
                return
            if route == "/v1/audio/transcriptions":
                self._openai()
                return
            if route == "/jobs":
                self._create_job()
                return
            self._reject(404)

        def _dictation(self) -> None:
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

        def _openai(self) -> None:
            if not self._authorized():
                self._reject(401, "missing or wrong token")
                return
            if jobs_queue() is None or getattr(svc, "asr", None) is None:
                self._not_ready()
                return
            body = self._body(max_mb() * 1024 * 1024)
            if body is None:
                return
            ctype = self.headers.get("Content-Type") or ""
            try:
                fields = _parse_multipart(body, ctype)
            except (ValueError, TypeError) as e:
                self._json(400, {"ok": False, "error": f"bad multipart: {e}"})
                return
            if "file" not in fields or not fields["file"][1]:
                self._json(400, {"ok": False, "error": "file field is missing"})
                return
            import asr as asr_mod
            try:
                asr_mod.check_model(_form_text(fields, "model", ""),
                                    str(cfg.get("model") or ""))
            except asr_mod.ModelNotInstalled as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            prompt = _form_text(fields, "prompt", "")
            if len(prompt) > PROMPT_MAX_CHARS:
                self._json(400, {"ok": False,
                                 "error": (f"prompt is longer than "
                                           f"{PROMPT_MAX_CHARS} characters")})
                return
            raw_temperature = _form_text(fields, "temperature", "")
            temperature = None
            if raw_temperature:
                try:
                    temperature = float(raw_temperature)
                except ValueError:
                    self._json(400, {"ok": False,
                                     "error": "temperature must be a number"})
                    return
                if not 0.0 <= temperature <= 1.0:
                    self._json(400, {"ok": False,
                                     "error": "temperature must be 0.0..1.0"})
                    return
            fmt = (_form_text(fields, "response_format", "json") or "json").lower()
            if fmt == "verbose_json":
                fmt = "json"
            if fmt not in ("json", "text", "srt", "vtt"):
                self._json(400, {"ok": False, "error": "unknown response_format"})
                return
            language = _form_text(fields, "language", "")
            if not service_idle():
                self._json(409, {"ok": False, "error": "dictation in progress"})
                return
            filename, data = fields["file"]
            suffix = os.path.splitext(filename)[1] or ".bin"
            tmp = ""
            try:
                fd, tmp = tempfile.mkstemp(prefix="_file_in_", suffix=suffix,
                                           dir=LOG_DIR)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                try:
                    audio, duration = asr_mod.decode_audio_file(
                        tmp, log, max_s=max_sync_s())
                except asr_mod.FileTooLong as e:
                    self._json(413, {
                        "ok": False,
                        "error": (f"file is {e.duration:.0f} s; the synchronous "
                                  f"limit is file_max_sync_s="
                                  f"{max_sync_s():.0f} s — post it to "
                                  "/jobs instead"),
                        "job_endpoint": "/jobs",
                    })
                    return
                segments = list(svc.asr.transcribe_array(
                    audio, duration, language=language or None,
                    initial_prompt=prompt or None, temperature=temperature))
                meta = {"language": language, "model": actual_model(),
                        "duration": duration}
                if fmt == "json":
                    verbose = _form_text(fields, "response_format", "") == "verbose_json"
                    if verbose:
                        from transcript import to_json
                        self._text(200, to_json(segments, meta),
                                   FILE_CONTENT_TYPES["json"])
                    else:
                        from transcript import Transcript
                        self._json(200, {"text": Transcript(segments).text})
                elif fmt == "text":
                    from transcript import Transcript
                    self._text(200, Transcript(segments).text,
                               "text/plain; charset=utf-8")
                else:
                    from transcript import render
                    self._text(200, render(segments, fmt, meta),
                               FILE_CONTENT_TYPES[fmt])
            except Exception as e:  # report, never crash the server
                log.exception("HTTP file transcription error: %s", e)
                self._json(500, {"ok": False, "error": str(e)})
            finally:
                if tmp:
                    with contextlib.suppress(OSError):
                        os.remove(tmp)

        def _create_job(self) -> None:
            if not self._authorized():
                self._reject(401, "missing or wrong token")
                return
            body = self._body(max_mb() * 1024 * 1024)
            if body is None:
                return
            ctype = self.headers.get("Content-Type") or ""
            language = ""
            model = ""
            formats: list[str] = []
            source = ""
            temporary = False
            try:
                if ctype.split(";")[0].strip().lower().startswith("multipart/"):
                    fields = _parse_multipart(body, ctype)
                    if "file" not in fields:
                        self._json(400, {"ok": False,
                                         "error": "file field is missing"})
                        return
                    filename, data = fields["file"]
                    suffix = os.path.splitext(filename)[1] or ".bin"
                    fd, source = tempfile.mkstemp(prefix="_file_job_",
                                                  suffix=suffix, dir=LOG_DIR)
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    temporary = True
                    language = _form_text(fields, "language", "")
                    model = _form_text(fields, "model", "")
                    formats = [f.strip().lower() for f in
                               _form_text(fields, "formats", "").replace(
                                   " ", ",").split(",") if f.strip()]
                else:
                    payload = json.loads(body.decode("utf-8"))
                    source = str(payload.get("path") or "")
                    language = str(payload.get("language") or "")
                    model = str(payload.get("model") or "")
                    formats = [str(f).lower() for f in payload.get("formats") or []]
            except (ValueError, TypeError, KeyError) as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            import asr as asr_mod
            queue = jobs_queue()
            if queue is None:
                if temporary and source:
                    with contextlib.suppress(OSError):
                        os.remove(source)
                self._not_ready()
                return
            try:
                job = queue.submit(source, language=language, model=model,
                                   formats=formats, temporary=temporary)
            except asr_mod.ModelNotInstalled as e:
                if temporary and source:
                    with contextlib.suppress(OSError):
                        os.remove(source)
                self._json(400, {"ok": False, "error": str(e)})
                return
            except (OSError, ValueError) as e:
                if temporary and source:
                    with contextlib.suppress(OSError):
                        os.remove(source)
                self._json(403 if "root" in str(e) or "allowed" in str(e) else 400,
                           {"ok": False, "error": str(e)})
                return
            self._json(202, {"job_id": job.id, "state": job.state},
                       headers={"Location": "/jobs/" + job.id})

        # -- DELETE -----------------------------------------------------
        def do_DELETE(self) -> None:
            route = urllib.parse.urlsplit(self.path).path
            if not route.startswith("/jobs/"):
                self._reject(404)
                return
            if not self._authorized():
                self._reject(401, "missing or wrong token")
                return
            job_id = [p for p in route.split("/") if p][1]
            queue = jobs_queue()
            if queue is None:
                self._not_ready()
                return
            if queue.get(job_id) is None:
                self._reject(404, "job not found")
                return
            if not queue.cancel(job_id):
                self._json(409, {"ok": False, "error": "job is already finished"})
                return
            self._json(200, {"ok": True, "job_id": job_id})

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
    log.info("HTTP API: http://127.0.0.1:%d/ (token required)", port)
    return server
