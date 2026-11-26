"""Local dictation history: a capped JSONL ring, opt-in, never in the log.

One record per line: {"ts": epoch, "text": ..., "chars": n, "app": name,
"way": "insert" or "copy", "audio": wav name}. The file lives next to
config.json and is written atomically. The text is deliberately never
logged: this module keeps no logger at all.

The sound is a separate opt-in: when set, the take is saved as a 16-bit
PCM WAV in ``history_audio/`` beside the JSONL, capped by total bytes.
A WAV goes away with its record, with a cleared history, when the ring
drops the record, and when the person turns the option off.
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
AUDIO_DIR_NAME = "history_audio"
MAX_AUDIO_BYTES = 200 * 1024 * 1024   # ~100 min of 16 kHz 16-bit mono


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
        return datetime.date.fromtimestamp(float(stamp))
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

    def add(self, text, app="", way=WAY_INSERT, wav=None):
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            items = self._read()
            ts = time.time()
            record = {"ts": ts, "text": text, "chars": len(text),
                      "way": normalize_way(way)}
            if app:
                record["app"] = str(app)[:80]
            name = self._save_wav(wav, ts) if wav else ""
            if name:
                record["audio"] = name
            items.append(record)
            dropped = items[:-MAX_RECORDS] if len(items) > MAX_RECORDS else []
            items = items[-MAX_RECORDS:]
            self._items = items
            self._write(items)
            for old in dropped:
                self._remove_audio(old)
            self._prune_audio(items)

    def delete(self, index):
        with self._lock:
            items = self._read()
            if not 0 <= index < len(items):
                return
            removed = items.pop(index)
            self._items = items
            self._write(items)
            self._remove_audio(removed)

    def replace(self, ts, text, confidence=None):
        """Replace one record's text and confidence reading; its WAV stays.

        The record is addressed by its epoch stamp, not by a list position:
        a dictation may append (and the cap may drop the oldest) while a
        wider pass runs, so an index captured at the click can point at
        another record. Returns False when ``text`` is empty or no record
        carries this stamp. ``confidence`` is the pass's own reading in
        0..1; None removes a stale one instead of keeping a number for a
        pass that was never measured.
        """
        text = (text or "").strip()
        if not text:
            return False
        try:
            wanted = float(ts)
        except (TypeError, ValueError):
            return False
        with self._lock:
            items = self._read()
            for record in reversed(items):
                try:
                    stamp = float(record.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if stamp != wanted:
                    continue
                record["text"] = text
                record["chars"] = len(text)
                if confidence is None:
                    record.pop("confidence", None)
                else:
                    record["confidence"] = round(
                        max(0.0, min(1.0, float(confidence))), 4)
                self._items = items
                self._write(items)
                return True
        return False

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
            self._clear_audio()

    # -- dictation sound ---------------------------------------------------
    @property
    def audio_dir(self):
        """The WAV folder beside this history file."""
        return os.path.join(os.path.dirname(self.path), AUDIO_DIR_NAME)

    def record_audio_path(self, record):
        """The WAV path a record names, or "" when it has none.

        Only a plain file name is accepted: a hand-edited record can never
        point outside the audio folder.
        """
        name = str((record or {}).get("audio") or "")
        if not name or os.path.basename(name) != name:
            return ""
        return os.path.join(self.audio_dir, name)

    def drop_audio(self):
        """Delete every stored WAV and forget the links; the text stays.

        Called when the person turns the audio option off: the sound is the
        heavy part, and switching it off is a request to keep the text only.
        """
        with self._lock:
            self._clear_audio()
            items = self._read()
            changed = False
            for record in items:
                if record.pop("audio", None) is not None:
                    changed = True
            if changed:
                self._items = items
                self._write(items)

    def _save_wav(self, wav, ts):
        """Write one take and return its plain file name, or "" on failure."""
        folder = self.audio_dir
        try:
            os.makedirs(folder, exist_ok=True)
            name = self._wav_name(folder, ts)
            with open(os.path.join(folder, name), "wb") as f:
                f.write(wav)
        except (OSError, TypeError):
            # A bad payload loses the sound, never the text record.
            return ""
        return name

    def _wav_name(self, folder, ts):
        """A name carrying the moment: milliseconds since the epoch, then a counter."""
        base = str(int(ts * 1000))
        name = base + ".wav"
        counter = 2
        while os.path.exists(os.path.join(folder, name)):
            name = f"{base}-{counter}.wav"
            counter += 1
        return name

    def _remove_audio(self, record):
        name = str((record or {}).get("audio") or "")
        if not name or os.path.basename(name) != name:
            return
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.audio_dir, name))

    def _clear_audio(self):
        folder = self.audio_dir
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, name))
        with contextlib.suppress(OSError):
            os.rmdir(folder)

    def _prune_audio(self, items):
        """Keep the WAV folder under MAX_AUDIO_BYTES, oldest first.

        A pruned file's record stays and keeps its text; only the ``audio``
        link goes, so nothing claims a sound that is no longer there. The
        order is the record order: what the ring already dropped (an
        orphaned file) goes before the oldest linked take.
        """
        folder = self.audio_dir
        try:
            names = os.listdir(folder)
        except OSError:
            return
        sizes = {}
        for name in names:
            if not name.endswith(".wav"):
                continue
            try:
                sizes[name] = os.path.getsize(os.path.join(folder, name))
            except OSError:
                continue
        total = sum(sizes.values())
        if total <= MAX_AUDIO_BYTES:
            return
        linked = {str(record.get("audio") or "") for record in items}
        order = [name for name in sizes if name not in linked]
        order += [str(record.get("audio") or "") for record in items
                  if str(record.get("audio") or "") in sizes]
        changed = False
        for name in order:
            if total <= MAX_AUDIO_BYTES:
                break
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                continue
            total -= sizes[name]
            for record in items:
                if str(record.get("audio") or "") == name:
                    record.pop("audio", None)
                    changed = True
        if changed:
            self._write(items)

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
