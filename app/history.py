"""Local dictation history: a capped JSONL ring, opt-in, never in the log.

One record per line: {"ts": epoch, "text": ..., "chars": n, "app": name,
"way": "insert" or "copy"}. The file lives next to config.json and is
written atomically. The text is deliberately never logged: this module
keeps no logger at all.
"""
import contextlib
import datetime
import json
import os
import threading
import time

MAX_RECORDS = 100
UNDO_WINDOW_S = 30
WAY_INSERT = "insert"
WAY_COPY = "copy"


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


def normalize_way(way):
    """One of the two directions; anything but "copy" reads as insert."""
    return WAY_COPY if str(way or "") == WAY_COPY else WAY_INSERT


def record_way(record):
    """The direction of one entry; old entries without the field are inserts."""
    return normalize_way((record or {}).get("way"))


def _local_date(stamp):
    """The calendar date of an epoch stamp on this machine, or None."""
    try:
        return datetime.date(*time.localtime(float(stamp))[:3])
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def history_filters(record, query="", app="", days=0, way="", now=None):
    """True when one entry passes the phrase and the three list filters.

    Pure: no Tk, no files. ``query`` is a case-insensitive phrase in the
    text or in the application name (the same rule as the settings table);
    ``app`` is an exact match on the stored name, empty for all; ``days``
    counts calendar days back from today (1 = today, 7 = today and the six
    days before, 0 = no limit); ``way`` is insert, copy or empty for both.
    ``now`` fixes the clock for the tests.
    """
    entry = record or {}
    phrase = str(query or "").strip().casefold()
    if phrase:
        text = str(entry.get("text") or "").casefold()
        name = str(entry.get("app") or "").casefold()
        if phrase not in text and phrase not in name:
            return False
    wanted_app = str(app or "")
    if wanted_app and str(entry.get("app") or "") != wanted_app:
        return False
    wanted_way = str(way or "")
    if wanted_way and record_way(entry) != wanted_way:
        return False
    try:
        span = int(days or 0)
    except (TypeError, ValueError):
        span = 0
    if span > 0:
        when = _local_date(entry.get("ts"))
        today = _local_date(time.time() if now is None else now)
        if when is None or today is None:
            return False
        cutoff = today - datetime.timedelta(days=span - 1)
        if when < cutoff or when > today:
            return False
    return True


def records_matching(records, query="", app="", days=0, way="", now=None):
    """The entries passing the filters, in their original order."""
    return [record for record in (records or [])
            if history_filters(record, query=query, app=app, days=days,
                               way=way, now=now)]


class History:
    """Thread-safe writer; readers get a snapshot, never a live list."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._items = None

    def load(self):
        with self._lock:
            return list(self._read())

    def add(self, text, app="", way=WAY_INSERT):
        text = (text or "").strip()
        if not text:
            return
        record = {"ts": time.time(), "text": text, "chars": len(text),
                  "way": normalize_way(way)}
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
