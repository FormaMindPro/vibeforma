"""Local usage statistics: daily counters in one JSON file.

The file lives beside config.json and is written atomically: a temp file
then os.replace. Only numbers are kept -- never the dictation text. The
dictation service calls record() once per finished phrase; the settings
tab (app/stats_ui.py) reads and resets the file.
"""
import contextlib
import json
import os
import threading
import time

# Ten years of daily rows still make a tiny file; the cap only stops a
# clock accident from growing it without bound.
MAX_DAYS = 3660
FIELDS = ("dictations", "words", "chars", "seconds")


def stats_path(service_dir):
    return os.path.join(service_dir, "stats.json")


def local_day(ts):
    """The local calendar day of the epoch time, YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def empty_day():
    return {"dictations": 0, "words": 0, "chars": 0, "seconds": 0.0}


class Stats:
    """Thread-safe daily counters; readers get a snapshot, never a live dict."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def load(self):
        """Totals and per-day rows, days in ascending order."""
        with self._lock:
            days = self._read()
        total = empty_day()
        rows = []
        for date in sorted(days):
            row = {key: days[date][key] for key in FIELDS}
            rows.append({"date": date, **row})
            for key in FIELDS:
                total[key] += row[key]
        total["seconds"] = round(total["seconds"], 3)
        return {"total": total, "days": rows}

    def record(self, chars, words, seconds, ts=None):
        """Add one finished dictation to its local day; a broken file is reset."""
        when = time.time() if ts is None else float(ts)
        date = local_day(when)
        with self._lock:
            days = self._read()
            row = days.get(date) or empty_day()
            row["dictations"] += 1
            row["words"] += _count(words)
            row["chars"] += _count(chars)
            row["seconds"] = round(row["seconds"] + _seconds(seconds), 3)
            days[date] = row
            days = dict(sorted(days.items())[-MAX_DAYS:])
            self._write(days)

    def reset(self):
        """Forget every counter: the next load reads an empty file."""
        with self._lock:
            for path in (self.path, self.path + ".tmp"):
                with contextlib.suppress(OSError):
                    os.remove(path)

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return {}
        raw = payload.get("days") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return {}
        days = {}
        for date, row in raw.items():
            if not _valid_date(date) or not isinstance(row, dict):
                continue
            days[date] = {
                "dictations": _count(row.get("dictations")),
                "words": _count(row.get("words")),
                "chars": _count(row.get("chars")),
                "seconds": _seconds(row.get("seconds")),
            }
        return days

    def _write(self, days):
        tmp = self.path + ".tmp"
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"days": days}, f, ensure_ascii=False,
                          indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.remove(tmp)


def _valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _seconds(value):
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0
