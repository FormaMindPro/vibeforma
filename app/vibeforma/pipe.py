"""Local CLI channel: a named pipe for the current user, no port, no token.

The CLI is a client of the running service, never a second process that loads
the model. The pipe is ``\\\\.\\pipe\\VibeForma-<user>`` with a DACL that grants
the owner alone, so a harness needs neither the HTTP API nor a secret.
Protocol: one JSON request line in, JSON event lines out, the last one has
``event`` ``done`` or ``error``.
"""
from __future__ import annotations

import contextlib
import ctypes
import getpass
import json
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

PIPE_PREFIX = "\\\\.\\pipe\\VibeForma-"
# One batch summary of hundreds of files is one JSON line: the line limit is
# 4 MiB. The per-instance buffer is not the line limit: 255 instances at 4 MiB
# would ask the kernel for gigabytes. Reads and writes go in 4 KiB chunks, so
# a 64 KiB buffer is enough for the stream and the limit is enforced in code.
PIPE_BUFFER = 64 * 1024
MAX_LINE = 4 * 1024 * 1024

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231


class PipeUnavailable(RuntimeError):
    """No service is listening on this user's pipe."""


def pipe_name(user: str | None = None) -> str:
    """The pipe address for this user; a harness reads it from location.json."""
    name = str(user or os.environ.get("USERNAME") or "").strip()
    if not name:
        with contextlib.suppress(Exception):
            name = getpass.getuser()
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                   for ch in (name or "user"))
    return PIPE_PREFIX + safe


def _kernel32() -> Any:
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    lib.CreateNamedPipeW.restype = wintypes.HANDLE
    lib.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    lib.ConnectNamedPipe.restype = wintypes.BOOL
    lib.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    lib.ReadFile.restype = wintypes.BOOL
    lib.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    lib.WriteFile.restype = wintypes.BOOL
    lib.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    lib.CreateFileW.restype = wintypes.HANDLE
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    lib.CloseHandle.restype = wintypes.BOOL
    return lib


def _owner_only_security() -> ctypes.c_void_p:
    """A security descriptor whose DACL grants full access to the owner only.

    ``OW`` is the Owner Rights SID: the object owner is the user running the
    service, so the pipe is open to that user and nobody else.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD()
    ok = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:P(A;;GA;;;OW)", 1, ctypes.byref(descriptor), ctypes.byref(size))
    if not ok:
        return ctypes.c_void_p()
    return descriptor


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


def _read_line(handle: int, kernel: Any) -> bytes:
    """Read one newline-terminated UTF-8 line from a pipe."""
    buffer = bytearray()
    while len(buffer) < MAX_LINE:
        chunk = ctypes.create_string_buffer(4096)
        read = wintypes.DWORD()
        ok = kernel.ReadFile(handle, chunk, 4096, ctypes.byref(read), None)
        if not ok or read.value == 0:
            break
        buffer.extend(chunk.raw[:read.value])
        if b"\n" in buffer:
            break
    return bytes(buffer)


def _write_line(handle: int, payload: dict[str, Any], kernel: Any) -> bool:
    body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    written = wintypes.DWORD()
    ok = kernel.WriteFile(handle, body, len(body), ctypes.byref(written), None)
    return bool(ok) and written.value == len(body)


class PipeServer:
    """One accept loop, one thread per client, JSON lines both ways."""

    def __init__(self, handler: Callable[[dict[str, Any], Callable[[dict[str, Any]],
                                                                   None]], dict[str, Any]],
                 log: Any = None, name: str | None = None) -> None:
        self.handler = handler
        self.log = log
        self.name = name or pipe_name()
        self._running = False
        self._thread = None
        self._current = 0
        self._ready = threading.Event()
        self._listening = False

    def _log(self, level: str, message: str, *args: Any) -> None:
        if self.log is not None:
            getattr(self.log, level, self.log.debug)(message, *args)

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="vibeforma-pipe")
        self._thread.start()
        # Return only once the first instance exists: the caller publishes the
        # pipe address and a client must not race a channel that is not there.
        self._ready.wait(5.0)
        return self._listening

    def stop(self) -> None:
        """Stop accepting; a blocked ConnectNamedPipe is released by a client.

        Closing the pending instance from this thread would wait for the
        connect to finish, so a short dummy connection does it instead and the
        accept loop closes the instance itself.
        """
        if not self._running:
            return
        self._running = False
        if not self._current:
            return
        kernel = _kernel32()
        client = kernel.CreateFileW(self.name, GENERIC_READ | GENERIC_WRITE, 0,
                                    None, OPEN_EXISTING, 0, None)
        if client not in (INVALID_HANDLE_VALUE, 0, None):
            kernel.CloseHandle(client)

    def _accept_loop(self) -> None:
        kernel = _kernel32()
        descriptor = _owner_only_security()
        while self._running:
            attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes),
                                        descriptor, False)
            handle = kernel.CreateNamedPipeW(
                self.name, PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES, PIPE_BUFFER, PIPE_BUFFER, 0,
                ctypes.byref(attrs))
            if handle == INVALID_HANDLE_VALUE or not handle:
                self._log("warning", "Pipe: CreateNamedPipe failed (%s)",
                          ctypes.get_last_error())
                self._listening = False
                self._ready.set()
                break
            self._current = handle
            self._listening = True
            self._ready.set()
            connected = kernel.ConnectNamedPipe(handle, None)
            if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                kernel.CloseHandle(handle)
                self._current = 0
                if not self._running:
                    break
                continue
            threading.Thread(target=self._serve, args=(handle, kernel),
                             daemon=True).start()
            self._current = 0
        self._running = False

    def _serve(self, handle: int, kernel: Any) -> None:
        try:
            raw = _read_line(handle, kernel)
            if not raw.strip():
                return
            payload = json.loads(raw.decode("utf-8"))
            response = self.handler(payload, lambda event: _write_line(
                handle, event, kernel))
            _write_line(handle, response, kernel)
        except Exception as e:  # noqa: BLE001 - one bad request never stops the channel
            self._log("debug", "Pipe request failed: %s", e)
            with contextlib.suppress(Exception):
                _write_line(handle, {"event": "error", "ok": False,
                                     "error": str(e)}, kernel)
        finally:
            with contextlib.suppress(Exception):
                kernel.CloseHandle(handle)


def request(payload: dict[str, Any], *, name: str | None = None,
            on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Send one request and follow its events until the final answer."""
    if os.name != "nt":
        raise PipeUnavailable("the pipe channel is Windows-only")
    kernel = _kernel32()
    handle = kernel.CreateFileW(name or pipe_name(),
                                GENERIC_READ | GENERIC_WRITE, 0, None,
                                OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE or not handle:
        raise PipeUnavailable("VibeForma is not running")
    try:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        written = wintypes.DWORD()
        if not kernel.WriteFile(handle, body, len(body), ctypes.byref(written),
                                None):
            raise PipeUnavailable("VibeForma did not accept the request")
        while True:
            raw = _read_line(handle, kernel)
            if not raw.strip():
                raise PipeUnavailable("VibeForma closed the pipe")
            message = json.loads(raw.decode("utf-8"))
            event = message.get("event")
            # A command that answers at once (cancel_batch) has no terminal
            # event: a message without one is the answer itself.
            if event in ("done", "error") or event is None:
                return message
            if on_event is not None:
                on_event(message)
    finally:
        with contextlib.suppress(Exception):
            kernel.CloseHandle(handle)
