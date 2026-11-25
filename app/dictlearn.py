"""Dictionary learning: a pair dialog and repeated corrections.

A pair "as heard -> as needed" enters the user dictionary only after an
explicit confirmation: the modal dialog, the history entry or the HUD
draft. Corrections made in the panel draft are also kept locally, word by
word; when the same replacement repeats, the settings window offers it -
it still waits for the person. Nothing here is applied automatically, and
nothing ever leaves this computer.

The store is corrections.json beside config.json: a list of pairs with a
count, written atomically under a lock. Only the words that changed are
kept, never the whole dictated text.
"""
import contextlib
import difflib
import json
import os
import threading

import dictionary
from config import DATA_DIR

MIN_COUNT = 2          # a suggestion is offered from this many repeats
MAX_PAIRS = 200        # the store stops growing silently
MAX_FROM = dictionary.MAX_FROM


def corrections_path(data_dir=None):
    """corrections.json in DATA_DIR (or the given folder)."""
    return os.path.join(data_dir or DATA_DIR, "corrections.json")


def _tokens(text):
    return str(text or "").split()


def _short(text):
    """One line out of the recognized text, capped like a dictionary term."""
    return " ".join(_tokens(text))[:MAX_FROM]


def diff_pairs(original, edited):
    """Word-level replacements: [(as heard, as needed)] from a draft edit.

    Only a replace block yields a pair: a pure insertion or deletion has no
    "as heard" side to learn. An empty result means nothing to learn.
    """
    before = _tokens(original)
    after = _tokens(edited)
    if not before or not after or before == after:
        return []
    pairs = []
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        entry = dictionary.normalize([{
            "from": " ".join(before[i1:i2]),
            "to": " ".join(after[j1:j2]),
        }])
        if entry:
            pairs.append((entry[0]["from"], entry[0]["to"]))
    return pairs


def _count(value):
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


class Corrections:
    """Thread-safe local store of repeated pairs; a broken file reads empty."""

    def __init__(self, path=None):
        self.path = path or corrections_path()
        self._lock = threading.Lock()

    def add(self, from_text, to_text):
        """Count one replacement; returns its new count or 0 when unusable."""
        entry = dictionary.normalize([{"from": from_text, "to": to_text}])
        if not entry:
            return 0
        src, dst = entry[0]["from"], entry[0]["to"]
        key = (src.casefold(), dst.casefold())
        with self._lock:
            rows = self._read()
            for row in rows:
                if (row["from"].casefold(), row["to"].casefold()) == key:
                    row["count"] += 1
                    row["to"] = dst
                    break
            else:
                rows.append({"from": src, "to": dst, "count": 1})
            self._write(rows)
            for row in rows:
                if (row["from"].casefold(), row["to"].casefold()) == key:
                    return row["count"]
        return 0

    def suggestions(self, threshold=MIN_COUNT):
        """Pairs seen often enough to offer; the most frequent come first."""
        with self._lock:
            rows = [dict(row) for row in self._read()]
        rows = [row for row in rows if row["count"] >= int(threshold)]
        rows.sort(key=lambda row: (-row["count"], row["from"].casefold()))
        return rows

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return []
        raw = payload.get("pairs") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            return []
        rows = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            entry = dictionary.normalize([item])
            if not entry:
                continue
            src, dst = entry[0]["from"], entry[0]["to"]
            key = (src.casefold(), dst.casefold())
            if key in seen:
                continue
            seen.add(key)
            rows.append({"from": src, "to": dst,
                         "count": _count(item.get("count"))})
            if len(rows) >= MAX_PAIRS:
                break
        return rows

    def _write(self, rows):
        tmp = self.path + ".tmp"
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pairs": rows}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.remove(tmp)


def collect_correction(original, edited, path=None):
    """Learn the pairs of one draft edit; storage never breaks the caller."""
    pairs = diff_pairs(original, edited)
    if not pairs:
        return pairs
    store = Corrections(path)
    for src, dst in pairs:
        with contextlib.suppress(Exception):
            store.add(src, dst)
    return pairs


def suggestions(path=None, threshold=MIN_COUNT):
    """Frequent pairs for the Dictionary tab; a broken store means none."""
    try:
        return Corrections(path).suggestions(threshold)
    except Exception:  # noqa: BLE001 - the settings tab must survive any store failure
        return []


def frequency(path=None):
    """How often each target form was corrected: {casefolded ``to``: count}.

    The recognition prompt ranks dictionary terms by this before the token
    budget is filled, so the terms a person fixes most often reach every
    window first. A missing or broken store means no ranking, never a failed
    transcription.
    """
    try:
        rows = Corrections(path).suggestions(1)
    except Exception:  # noqa: BLE001 - a broken store never breaks recognition
        return {}
    weights = {}
    for row in rows:
        key = row["to"].casefold()
        weights[key] = weights.get(key, 0) + row["count"]
    return weights


def add_pair(cfg, save, from_text, to_text):
    """The one place a confirmed pair enters the dictionary.

    Both sides are required: the pair dialog is about a replacement, while
    a term without one belongs to the Dictionary tab's own Add. The pair is
    normalized with the same rules; the list is de-duplicated
    case-insensitively. save() persists the config when given. Returns
    False when the pair is unusable or cfg is missing.
    """
    if not str(from_text or "").strip() or not str(to_text or "").strip():
        return False
    entry = dictionary.normalize([{"from": from_text, "to": to_text}])
    if cfg is None or not entry:
        return False
    cfg["dictionary"] = dictionary.normalize(
        list(cfg.get("dictionary") or []) + entry)
    if save is not None:
        save()
    return True


def _default_save(cfg):
    import config
    config.save(cfg)


def pair_dialog(parent, tr, from_text, cfg=None, save=None):
    """Modal "as heard -> as needed" window; True when a pair was added.

    from is prefilled with the recognized text cut to a term; to starts
    empty. The pair goes through dictionary.normalize, so an empty or
    identical value is refused with a hint. The dialog must never break
    the caller: a Tk failure simply returns False.
    """
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001 - a headless environment has no dialog
        return False
    if cfg is None:
        return False
    try:
        win = tk.Toplevel(parent)
    except Exception:  # noqa: BLE001 - a destroyed parent means no dialog
        return False
    win.title(tr("dictlearn.title"))
    win.transient(parent)
    win.resizable(False, False)
    with contextlib.suppress(Exception):
        win.attributes("-topmost", True)
    body = tk.Frame(win, padx=16, pady=12)
    body.pack(fill="both", expand=True)
    tk.Label(body, text=tr("dictlearn.from"), anchor="w").grid(
        row=0, column=0, sticky="w")
    tk.Label(body, text=tr("dictlearn.to"), anchor="w").grid(
        row=1, column=0, sticky="w", pady=(8, 0))
    v_from = tk.StringVar(value=_short(from_text))
    v_to = tk.StringVar(value="")
    entry_from = tk.Entry(body, textvariable=v_from, width=36)
    entry_to = tk.Entry(body, textvariable=v_to, width=36)
    entry_from.grid(row=0, column=1, sticky="we", padx=(10, 0))
    entry_to.grid(row=1, column=1, sticky="we", padx=(10, 0), pady=(8, 0))
    status = tk.Label(body, text="", anchor="w", justify="left",
                      wraplength=380)
    status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
    buttons = tk.Frame(body)
    buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
    added = {"value": False}

    def commit(_event=None):
        persist = save if save is not None else (lambda: _default_save(cfg))
        try:
            ok = add_pair(cfg, persist, v_from.get(), v_to.get())
        except Exception as e:  # noqa: BLE001 - a failed write keeps the dialog open
            status.configure(text=tr("dictlearn.save_failed", detail=e))
            return
        if not ok:
            status.configure(text=tr("dictlearn.need"))
            return
        added["value"] = True
        win.destroy()

    def cancel(_event=None):
        win.destroy()

    tk.Button(buttons, text=tr("dictlearn.add"),
              command=commit).pack(side="left")
    tk.Button(buttons, text=tr("dictlearn.cancel"),
              command=cancel).pack(side="left", padx=(8, 0))
    win.bind("<Return>", commit)
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)
    with contextlib.suppress(Exception):
        win.grab_set()
        (entry_to if v_from.get() else entry_from).focus_set()
    try:
        parent.wait_window(win)
    except Exception:  # noqa: BLE001 - a destroyed parent leaves nothing to wait for
        with contextlib.suppress(Exception):
            win.destroy()
    return added["value"]
