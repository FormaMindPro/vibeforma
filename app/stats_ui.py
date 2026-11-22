"""Statistics tab: totals, the last 30 days and a guarded reset.

The settings window owns the nav rail and the pane; this module owns the
tab's layout. Counters come from app/stats.py and never leave the machine.
"""
import tkinter as tk
from tkinter import messagebox, ttk

BG = "#16181D"
FG = "#D5D9E0"
DIM = "#9AA3B0"
FIELD_BG = "#22262E"
LINE = "#242830"
ROW_BG = "#1B1E24"
BUTTON_BG = "#22262E"
BUTTON_ACTIVE = "#2A2F38"
OK_FG = "#9FE0A8"

SHOWN_DAYS = 30


def format_number(value):
    """A counter with space-grouped thousands: 1234567 -> '1 234 567'."""
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}".replace(",", " ")


def format_duration(seconds):
    """Recording time as m:ss, or h:mm:ss from one hour up."""
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def table_rows(data, limit=SHOWN_DAYS):
    """Newest first, at most `limit` recorded days, cells as display strings."""
    days = list((data or {}).get("days") or [])
    rows = []
    for row in reversed(days[-limit:]):
        rows.append((
            str(row.get("date") or ""),
            format_number(row.get("dictations")),
            format_number(row.get("words")),
            format_number(row.get("chars")),
            format_duration(row.get("seconds")),
        ))
    return rows


def build_stats_tab(parent, tr, px, store):
    """Build the Statistics page inside `parent`; return its refresh function."""
    tk.Label(parent, text=tr("tabs.stats"), font=("Segoe UI", 15, "bold"),
             fg=FG, bg=BG).pack(anchor="w", padx=px(18),
                                pady=(px(18), px(2)))
    tk.Label(parent, text=tr("stats.sub"), font=("Segoe UI", 9), fg=DIM,
             bg=BG).pack(anchor="w", padx=px(18), pady=(0, px(8)))

    summary = tk.Frame(parent, bg=BG)
    summary.pack(fill="x", padx=px(18), pady=(0, px(6)))
    values = {}
    for column, (key, caption_key) in enumerate((
            ("dictations", "stats.total_dictations"),
            ("words", "stats.total_words"),
            ("chars", "stats.total_chars"),
            ("seconds", "stats.total_seconds"))):
        cell = tk.Frame(summary, bg=FIELD_BG)
        cell.pack(side="left", fill="x", expand=True,
                  padx=(0, px(8)) if column < 3 else 0)
        value = tk.Label(cell, text="0", font=("Segoe UI", 16, "bold"),
                         fg=FG, bg=FIELD_BG, anchor="w")
        value.pack(fill="x", padx=px(10), pady=(px(6), 0))
        tk.Label(cell, text=tr(caption_key), font=("Segoe UI", 9), fg=DIM,
                 bg=FIELD_BG, anchor="w").pack(
                     fill="x", padx=px(10), pady=(0, px(6)))
        values[key] = value

    note = tk.Label(parent, text=tr("stats.note"), font=("Segoe UI", 8),
                    fg=DIM, bg=BG, anchor="w", justify="left")
    note.pack(fill="x", padx=px(18), pady=(0, px(6)))

    # -- reset strip, pinned to the bottom like the history settings --------
    strip = tk.Frame(parent, bg=FIELD_BG)
    strip.pack(side="bottom", fill="x", padx=px(18), pady=(px(8), px(12)))
    tk.Frame(strip, bg=LINE, height=px(1)).pack(fill="x")
    row = tk.Frame(strip, bg=FIELD_BG)
    row.pack(fill="x", padx=px(10), pady=px(8))
    reset = tk.Button(row, text=tr("stats.reset"), command=lambda: _reset(),
                      bg=BUTTON_BG, fg=FG, relief="flat",
                      activebackground=BUTTON_ACTIVE, activeforeground=FG,
                      cursor="hand2", padx=px(8))
    reset.pack(side="left")
    if store is None:
        reset.configure(state="disabled")

    status = tk.Label(parent, text="", font=("Segoe UI", 9), fg=OK_FG,
                      bg=BG, anchor="w", justify="left")
    status.pack(side="bottom", fill="x", padx=px(18), pady=(0, px(2)))

    # -- table ---------------------------------------------------------------
    table_host = tk.Frame(parent, bg=BG)
    table_host.pack(fill="both", expand=True, padx=px(18), pady=(px(4), 0))
    columns = ("date", "dictations", "words", "chars", "seconds")
    tree = ttk.Treeview(table_host, columns=columns, show="headings",
                        height=14, style="Vibe.Treeview")
    for column, key, width, anchor in (
            ("date", "stats.col_date", 110, "w"),
            ("dictations", "stats.col_dictations", 110, "e"),
            ("words", "stats.col_words", 110, "e"),
            ("chars", "stats.col_chars", 110, "e"),
            ("seconds", "stats.col_seconds", 150, "e")):
        tree.heading(column, text=tr(key), anchor=anchor)
        tree.column(column, width=px(width), minwidth=px(60),
                    stretch=(column == "seconds"), anchor=anchor)
    tree.tag_configure("odd", background=BG, foreground=FG)
    tree.tag_configure("even", background=ROW_BG, foreground=FG)
    bar = ttk.Scrollbar(table_host, orient="vertical",
                        style="Vibe.Vertical.TScrollbar", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def refresh():
        data = {"total": {}, "days": []}
        if store is not None:
            try:
                data = store.load()
            except Exception:  # noqa: BLE001 - a broken file shows as empty
                data = {"total": {}, "days": []}
        total = data.get("total") or {}
        values["dictations"].configure(
            text=format_number(total.get("dictations")))
        values["words"].configure(text=format_number(total.get("words")))
        values["chars"].configure(text=format_number(total.get("chars")))
        values["seconds"].configure(
            text=format_duration(total.get("seconds")))
        rows = tree.get_children()
        if rows:
            tree.delete(*rows)
        table = table_rows(data)
        for index, row_values in enumerate(table):
            tree.insert("", "end", values=row_values,
                        tags=("odd" if index % 2 == 0 else "even",))
        status.configure(text="" if table else tr("stats.empty"))

    def _reset():
        if store is None:
            return
        if not messagebox.askyesno(
                tr("stats.reset"), tr("stats.reset_question"),
                parent=parent.winfo_toplevel()):
            return
        store.reset()
        refresh()
        status.configure(text=tr("stats.reset_done"))

    refresh()
    return refresh
