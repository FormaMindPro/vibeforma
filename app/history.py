"""Local dictation history: a capped JSONL ring, opt-in, never in the log.

One record per line: {"ts": epoch, "text": ..., "chars": n, "app": name}.
The file lives next to config.json and is written atomically. The text is
deliberately never logged: this module keeps no logger at all.
"""
import contextlib
import json
import os
import threading
import time

MAX_RECORDS = 100
UNDO_WINDOW_S = 30


def history_path(service_dir):
    return os.path.join(service_dir, "history.jsonl")


def undo_allowed(last, now, hwnd):
    """Why an automatic Ctrl+Z may run: ok | none | other_window | late."""
    if not last:
        return "none"
    if hwnd != last.get("hwnd"):
        return "other_window"
    if now - float(last.get("ts") or 0.0) > UNDO_WINDOW_S:
        return "late"
    return "ok"


class History:
    """Thread-safe writer; readers get a snapshot, never a live list."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._items = None

    def load(self):
        with self._lock:
            return list(self._read())

    def add(self, text, app=""):
        text = (text or "").strip()
        if not text:
            return
        record = {"ts": time.time(), "text": text, "chars": len(text)}
        if app:
            record["app"] = str(app)[:80]
        with self._lock:
            items = self._read()
            items.append(record)
            items = items[-MAX_RECORDS:]
            self._items = items
            self._write(items)

    def delete(self, index):
        with self._lock:
            items = self._read()
            if not 0 <= index < len(items):
                return
            del items[index]
            self._items = items
            self._write(items)

    def clear(self):
        with self._lock:
            self._items = []
            for path in (self.path, self.path + ".tmp"):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _read(self):
        if self._items is not None:
            return self._items
        items = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(record, dict)
                            and isinstance(record.get("text"), str)):
                        items.append(record)
        except OSError:
            pass
        self._items = items[-MAX_RECORDS:]
        return self._items

    def _write(self, items):
        tmp = self.path + ".tmp"
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                for record in items:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.remove(tmp)
