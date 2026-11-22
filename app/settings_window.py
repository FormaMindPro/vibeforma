"""Settings dialog (tkinter Toplevel) — lives on the HUD main thread.

Localized through i18n (en/ru shipped). A sidebar of eight sections
(Recording / Model & language / Dictionary / Snippets / History / Statistics /
Appearance / System) keeps the window compact no matter how many settings
land. The model tab switches models through a progress dialog (download ->
load -> remove old on request). Dictionary, snippets and history are local:
no cloud, no account.
"""
import contextlib
import ctypes
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dictionary
import dictlearn
import history
import i18n
import onboarding
import snippets
import snippets_ui
import stats_ui
import window_layout
import winfx
from config import DEFAULTS, LANG_NAMES, LANGS, MODEL_GB, MODELS, VERSION

BG = "#16181D"
FG = "#D5D9E0"
DIM = "#9AA3B0"
FIELD_BG = "#22262E"
SIDE_BG = "#121419"
LINE = "#242830"
ACCENT = "#2E86DE"
ACCENT_DARK = "#1B2532"


def dpi_factor(win):
    """Windows scaling factor (1.0 at 100%, 1.5 at 150%) for fixed-pixel sizes."""
    try:
        return max(1.0, win.winfo_fpixels("1i") / 96.0)
    except Exception:  # noqa: BLE001 - the window must survive any Tk failure
        return 1.0


def history_row(record):
    """Table cells for one saved dictation: when, chars, app, one-line text."""
    text = str(record.get("text") or "")
    stamp = time.strftime("%Y-%m-%d %H:%M",
                          time.localtime(float(record.get("ts") or 0)))
    return (stamp, int(record.get("chars") or len(text)),
            str(record.get("app") or ""), text.replace("\n", " "))


def history_matches(record, query):
    """True when the phrase is in the text or in the application name."""
    return history.history_filters(record, query=query)

_k32 = ctypes.WinDLL("kernel32")


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(_PMC), ctypes.c_ulong]


def process_ram_mb():
    """Private memory of the service process (model included), MB."""
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if _k32.K32GetProcessMemoryInfo(_k32.GetCurrentProcess(),
                                    ctypes.byref(pmc), pmc.cb):
        return int(pmc.PagefileUsage / (1024 * 1024))
    return 0


def phase_text(tr, p):
    """Localized model-switch phase, shared by the dialog and the inline label."""
    ph = p.get("phase")
    if ph == "download":
        name = p.get("name", "")
        return tr("dlg.phase_download", name=name, gb=MODEL_GB.get(name, "?"))
    if ph == "load":
        return tr("dlg.phase_load", name=p.get("name", ""))
    if ph == "remove":
        return tr("dlg.phase_remove", name=p.get("name", ""))
    if ph == "done":
        text = tr("dlg.phase_done", name=p.get("name", ""))
        if p.get("cache_note"):
            text += "\n" + tr("dlg.cache_note", name=p["cache_note"])
        return text
    if ph == "busy":
        return tr("dlg.phase_busy")
    if ph == "error":
        return tr("dlg.phase_error", detail=p.get("detail", ""))
    return "…"


class ModelSwitchDialog:
    """Progress dialog for a settings-driven model switch.

    The switch runs in a service thread that updates a plain dict; this
    dialog (HUD thread) polls it.
    """

    def __init__(self, master, progress, tr):
        self.progress = progress
        self.tr = tr
        px = lambda n: int(round(n * dpi_factor(master)))  # noqa: E731
        self.win = tk.Toplevel(master)
        self.win.title(self.tr("dlg.model_title"))
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        with contextlib.suppress(Exception):
            winfx.round_native(self.win)
            winfx.dark_title(self.win)
        tk.Label(self.win, text=self.tr("dlg.model_title"),
                 font=("Segoe UI", 11, "bold"), fg="#8FB8E8",
                 bg=BG).pack(fill="x", padx=px(16), pady=(px(12), px(4)))
        self.lbl = tk.Label(self.win, text="…", font=("Segoe UI", 10),
                            fg=FG, bg=BG, wraplength=px(330), justify="left")
        self.lbl.pack(fill="x", padx=px(16))
        self.bar = ttk.Progressbar(self.win, mode="indeterminate",
                                   length=px(330))
        self.bar.pack(fill="x", padx=px(16), pady=(px(8), px(4)))
        self.bar.start(12)
        self.btn = tk.Button(self.win, text=self.tr("dlg.close"),
                             state="disabled", command=self.win.destroy,
                             width=12, bg="#22262E", fg=FG, relief="flat",
                             activebackground="#2A2F38", activeforeground=FG)
        self.btn.pack(pady=(px(4), px(12)))
        with contextlib.suppress(Exception):
            self.win.geometry(f"+{master.winfo_rootx() + px(40)}"
                              f"+{master.winfo_rooty() + px(40)}")
        self.win.after(200, self._poll)

    def _text(self, p):
        return phase_text(self.tr, p)

    def _poll(self):
        with contextlib.suppress(Exception):
            if not self.win.winfo_exists():
                return
            p = dict(self.progress)
            self.lbl.configure(text=self._text(p))
            if p.get("phase") in ("done", "error", "busy"):
                self.bar.stop()
                self.btn.configure(state="normal")
                return   # stop polling; the window stays until user closes it
        self.win.after(200, self._poll)


class _ScrollPane:
    """A tab body that scrolls instead of clipping when the window is short."""

    def __init__(self, parent, px):
        self.px = px
        self._bar_shown = False
        self.host = tk.Frame(parent, bg=BG)
        self.canvas = tk.Canvas(self.host, bg=BG, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self.host, orient="vertical",
                                 style="Vibe.Vertical.TScrollbar",
                                 command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._item = self.canvas.create_window((0, 0), window=self.inner,
                                               anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._grow)
        self.canvas.bind("<Configure>", self._wide)
        self.host.bind("<Enter>", self._grab_wheel)
        self.host.bind("<Leave>", self._drop_wheel)

    def _grow(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_bar()

    def _wide(self, event):
        self.canvas.itemconfigure(self._item, width=event.width)
        self._sync_bar()

    def _sync_bar(self):
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need and not self._bar_shown:
            self.bar.pack(side="right", fill="y")
            self._bar_shown = True
        elif not need and self._bar_shown:
            self.bar.pack_forget()
            self._bar_shown = False

    def _grab_wheel(self, _event):
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _drop_wheel(self, _event):
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event):
        # the list boxes and text fields keep their own wheel behaviour
        if isinstance(event.widget, (tk.Listbox, tk.Text)):
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class _FillPane:
    """A section that fills the content area; its table scrolls inside."""

    def __init__(self, parent, px):
        self.host = tk.Frame(parent, bg=BG)
        self.inner = self.host


class _Toggle(tk.Canvas):
    """A switch drawn on a canvas: a pill track and a round knob."""

    TRACK_W = 40
    TRACK_H = 22
    KNOB = 16

    def __init__(self, parent, var, px):
        self.px = px
        self.var = var
        super().__init__(parent, width=px(self.TRACK_W),
                         height=px(self.TRACK_H), bg=BG,
                         highlightthickness=0, bd=0, cursor="hand2",
                         takefocus=1)
        self.bind("<Button-1>", self.invoke)
        self.bind("<space>", self.invoke)
        self.bind("<Return>", self.invoke)
        var.trace_add("write", self._sync)
        self._draw()

    def invoke(self, _event=None):
        self.var.set(not bool(self.var.get()))

    def _sync(self, *_args):
        with contextlib.suppress(tk.TclError):
            self._draw()

    def _draw(self):
        s = self.px
        on = bool(self.var.get())
        self.delete("all")
        track = ACCENT if on else "#3A4048"
        r = s(self.TRACK_H)
        self.create_oval(0, 0, r, s(self.TRACK_H), fill=track, outline="")
        self.create_oval(s(self.TRACK_W - self.TRACK_H), 0,
                         s(self.TRACK_W), s(self.TRACK_H),
                         fill=track, outline="")
        self.create_rectangle(s(self.TRACK_H / 2), 0,
                              s(self.TRACK_W - self.TRACK_H / 2),
                              s(self.TRACK_H), fill=track, outline="")
        left = s(self.TRACK_W - self.KNOB - 3) if on else s(3)
        top = s((self.TRACK_H - self.KNOB) / 2)
        self.create_oval(left, top, left + s(self.KNOB),
                         top + s(self.KNOB), fill="#F2F5F9", outline="")


class SettingsWindow:
    def __init__(self, master, svc, log, tab=None):
        self.svc = svc
        self.cfg = svc.cfg
        self.log = log
        self.master = master
        tr = lambda key, **kw: svc.tr(key, **kw)  # noqa: E731
        self.tr = tr
        self._px = lambda n: int(round(n * dpi_factor(self.win)))

        self.win = tk.Toplevel(master)
        self.win.title(self.tr("settings.title"))
        self.win.configure(bg=BG)
        self.win.resizable(True, True)
        self.win.minsize(self._px(window_layout.MIN_W),
                         self._px(window_layout.MIN_H))

        self.rows = {}   # shared by the row helpers below
        self._search_index = []   # (text, section key, label or None, fg)
        self._nav = {}
        self._shown = None
        self.snippets = snippets.normalize(self.cfg.get("snippets"))

        self._style_scrollbar()
        self._build_header()
        self._build_search()
        frames = self._create_panes()
        self._build_record_tab(frames["record"])
        self._build_model_tab(frames["model"])
        self._build_dictionary_tab(frames["dictionary"])
        self.snippets_tab = snippets_ui.build_snippets_tab(
            self.win, frames["snippets"], self._px, self.tr, self.cfg,
            self._set_snippets)
        self._build_history_tab(frames["history"])
        self._refresh_stats = stats_ui.build_stats_tab(
            frames["stats"], self.tr, self._px,
            getattr(self.svc, "stats", None))
        self._build_look_tab(frames["look"])
        self._build_system_tab(frames["system"])
        self._build_footer()
        self._fit_to_content(frames)
        self._show(tab if tab in self._scrolls else "record")

        self.win.bind("<Control-f>", lambda _e: self._focus_search())
        self.win.bind("<Control-F>", lambda _e: self._focus_search())
        self.win.bind("<Control-s>", lambda _e: self._save())
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        with contextlib.suppress(Exception):
            winfx.round_native(self.win)
            winfx.dark_title(self.win)

    # -- construction steps ------------------------------------------------
    def _style_scrollbar(self):
        style = ttk.Style(self.win)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        # clam draws the scrollbar from these elements; without the dark
        # border/light colors it falls back to a light native-looking track
        style.configure("Vibe.Vertical.TScrollbar", troughcolor=BG,
                        background="#3A4048", bordercolor=BG,
                        darkcolor=BG, lightcolor=BG, arrowcolor=DIM,
                        relief="flat", arrowsize=self._px(10))
        style.map("Vibe.Vertical.TScrollbar",
                  background=[("active", "#4A515C")])
        style.configure("Vibe.Treeview", background=BG, fieldbackground=BG,
                        foreground=FG, rowheight=self._px(24),
                        bordercolor=BG, lightcolor=BG, darkcolor=BG,
                        borderwidth=0, font=("Segoe UI", 9))
        style.configure("Vibe.Treeview.Heading", background=FIELD_BG,
                        foreground=DIM, relief="flat", borderwidth=0,
                        font=("Segoe UI", 9, "bold"))
        style.map("Vibe.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "white")])
        style.map("Vibe.Treeview.Heading",
                  background=[("active", "#2A2F38")])
        style.configure("Vibe.TCombobox", fieldbackground=FIELD_BG,
                        background=FIELD_BG, foreground=FG, arrowcolor=DIM,
                        bordercolor=BG, lightcolor=FIELD_BG,
                        darkcolor=FIELD_BG, padding=self._px(2),
                        font=("Segoe UI", 9))
        style.map("Vibe.TCombobox",
                  fieldbackground=[("readonly", FIELD_BG)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", FIELD_BG)],
                  selectforeground=[("readonly", FG)])
        self.win.option_add("*TCombobox*Listbox.background", FIELD_BG)
        self.win.option_add("*TCombobox*Listbox.foreground", FG)
        self.win.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.win.option_add("*TCombobox*Listbox.selectForeground", "white")

    def _build_header(self):
        head = self._header = tk.Frame(self.win, bg=BG)
        head.pack(fill="x", padx=self._px(18), pady=(self._px(12), 0))
        logo = tk.Canvas(head, width=self._px(24), height=self._px(24),
                         bg=BG, highlightthickness=0)
        logo.create_oval(0, 0, self._px(24), self._px(24),
                         fill=ACCENT, outline="")
        logo.create_text(self._px(12), self._px(12), text="V",
                         fill="white", font=("Segoe UI", 9, "bold"))
        logo.pack(side="left")
        tk.Label(head, text=self.tr("settings.header"),
                 font=("Segoe UI", 12, "bold"), fg=FG, bg=BG).pack(
                     side="left", padx=(self._px(10), self._px(8)))
        tk.Label(head, text=VERSION, font=("Segoe UI", 8), fg=DIM,
                 bg=FIELD_BG, padx=self._px(6), pady=self._px(1)).pack(
                     side="left")
        device = str(self.cfg.get("device", "cuda")).upper()
        lang = LANG_NAMES.get(str(self.cfg.get("language", "en")),
                              str(self.cfg.get("language", "en")))
        tk.Label(head, text=f"{device}  ·  {lang}", font=("Segoe UI", 9),
                 fg=DIM, bg=BG).pack(side="right")
        dot = tk.Canvas(head, width=self._px(10), height=self._px(10),
                        bg=BG, highlightthickness=0)
        dot.create_oval(self._px(1), self._px(1), self._px(9),
                        self._px(9), fill="#3FA34D", outline="")
        dot.pack(side="right", padx=(0, self._px(8)))

    def _build_search(self):
        row = self._search_row = tk.Frame(self.win, bg=BG)
        row.pack(fill="x", padx=self._px(18),
                 pady=(self._px(10), self._px(6)))
        tk.Label(row, text=self.tr("settings.search"), font=("Segoe UI", 9),
                 fg=DIM, bg=BG).pack(side="left")
        self.v_search = tk.StringVar(value="")
        entry = self.search_entry = tk.Entry(
            row, textvariable=self.v_search, width=30, bg=FIELD_BG, fg=FG,
            insertbackground=FG, relief="flat", highlightthickness=1,
            highlightbackground="#3A4048", highlightcolor=ACCENT)
        entry.pack(side="left", padx=(self._px(10), 0), ipady=self._px(2))
        entry.bind("<Return>", lambda _e: self._search())
        tk.Label(row, text="Ctrl+F", font=("Segoe UI", 8), fg="#5D6572",
                 bg=BG).pack(side="right")

    def _create_panes(self):
        main = self._main = tk.Frame(self.win, bg=BG)
        main.pack(fill="both", expand=True)
        rail = tk.Frame(main, bg=SIDE_BG, width=self._px(214))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        nav = tk.Frame(rail, bg=SIDE_BG)
        nav.pack(fill="x", pady=(self._px(12), 0))
        for key, title_key in (
                ("record", "tabs.record"), ("model", "tabs.model"),
                ("dictionary", "tabs.dictionary"),
                ("snippets", "tabs.snippets"), ("history", "tabs.history"),
                ("stats", "tabs.stats"),
                ("look", "tabs.look"), ("system", "tabs.system")):
            self._nav_item(nav, key, self.tr(title_key))
        self._local_card(rail)
        tk.Frame(main, bg=LINE, width=self._px(1)).pack(side="left", fill="y")
        content = tk.Frame(main, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        frames = {}
        self._scrolls = {}
        for key, _title in (
                ("record", None), ("model", None), ("dictionary", None),
                ("snippets", None), ("history", None), ("stats", None),
                ("look", None), ("system", None)):
            if key in ("history", "stats"):
                # The history and stats tables fill the pane; the page
                # itself must not scroll.
                pane = _FillPane(content, self._px)
            else:
                pane = _ScrollPane(content, self._px)
                pane.inner.columnconfigure(0, weight=1)
            self._scrolls[key] = pane
            frames[key] = pane.inner
        self._frame_key = {p.inner: key for key, p in self._scrolls.items()}
        return frames

    def _nav_item(self, parent, key, title):
        item = tk.Frame(parent, bg=SIDE_BG, cursor="hand2")
        item.pack(fill="x", pady=self._px(1))
        bar = tk.Frame(item, bg=SIDE_BG, width=self._px(3))
        bar.pack(side="left", fill="y")
        label = tk.Label(item, text=title, anchor="w", font=("Segoe UI", 10),
                         fg=DIM, bg=SIDE_BG, padx=self._px(14),
                         pady=self._px(9), cursor="hand2")
        label.pack(side="left", fill="x", expand=True)
        self._nav[key] = (item, bar, label)
        for widget in (item, label):
            widget.bind("<Button-1>", lambda _e, k=key: self._show(k))
            widget.bind("<Enter>", lambda _e, k=key: self._nav_hover(k, True))
            widget.bind("<Leave>", lambda _e, k=key: self._nav_hover(k, False))
        self._search_index.append((title, key, None, None))

    def _local_card(self, rail):
        card = tk.Frame(rail, bg=FIELD_BG)
        card.pack(side="bottom", fill="x", padx=self._px(12),
                  pady=self._px(12))
        tk.Label(card, text=self.tr("settings.local_title"),
                 font=("Segoe UI", 9, "bold"), fg=FG, bg=FIELD_BG,
                 anchor="w").pack(fill="x", padx=self._px(10),
                                  pady=(self._px(8), 0))
        tk.Label(card, text=self.tr("settings.local_note"),
                 font=("Segoe UI", 8), fg=DIM, bg=FIELD_BG, anchor="w",
                 justify="left", wraplength=self._px(168)).pack(
                     fill="x", padx=self._px(10),
                     pady=(self._px(2), self._px(8)))

    def _nav_hover(self, key, inside):
        if key == self._shown:
            return
        bg = "#181B21" if inside else SIDE_BG
        item, _bar, label = self._nav[key]
        item.configure(bg=bg)
        label.configure(bg=bg)

    def _paint_nav(self, key):
        for name, (item, bar, label) in self._nav.items():
            on = name == key
            bg = ACCENT_DARK if on else SIDE_BG
            item.configure(bg=bg)
            label.configure(bg=bg, fg=FG if on else DIM)
            bar.configure(bg=ACCENT if on else bg)

    def _show(self, key):
        if key not in self._scrolls or key == self._shown:
            return
        for pane in self._scrolls.values():
            pane.host.pack_forget()
        self._scrolls[key].host.pack(side="left", fill="both", expand=True)
        self._shown = key
        if key == "stats" and getattr(self, "_refresh_stats", None):
            self._refresh_stats()
        self._paint_nav(key)

    # -- row helpers -------------------------------------------------------
    def _pane_heading(self, f, title_key, sub_key):
        self.rows[f] = 0
        title = tk.Label(f, text=self.tr(title_key),
                         font=("Segoe UI", 15, "bold"), fg=FG, bg=BG)
        title.grid(row=0, column=0, columnspan=2, sticky="w",
                   padx=self._px(18), pady=(self._px(18), self._px(2)))
        tk.Label(f, text=self.tr(sub_key), font=("Segoe UI", 9), fg=DIM,
                 bg=BG, wraplength=self._px(520), justify="left").grid(
                     row=1, column=0, columnspan=2, sticky="w",
                     padx=self._px(18), pady=(0, self._px(4)))
        self.rows[f] = 2

    def _head(self, f, text):
        rows = self.rows.get(f, 0)
        if rows:
            tk.Frame(f, bg=LINE, height=self._px(1)).grid(
                row=rows, column=0, columnspan=2, sticky="we",
                padx=self._px(18), pady=(self._px(12), self._px(2)))
            rows += 1
        label = tk.Label(f, text=text, font=("Segoe UI", 9, "bold"),
                         fg=DIM, bg=BG)
        label.grid(row=rows, column=0, columnspan=2, sticky="w",
                   padx=self._px(18), pady=(self._px(6), self._px(2)))
        self.rows[f] = rows + 1
        self._search_index.append(
            (text, self._frame_key[f], label, DIM))

    def _auto_wrap(self, label, limit=430):
        label.configure(wraplength=self._px(limit), justify="left")

        def resize(event):
            target = max(self._px(160), event.width - self._px(2))
            if int(label.cget("wraplength") or 0) != target:
                label.configure(wraplength=target)

        label.bind("<Configure>", resize, add="+")

    def _row(self, f, label_text, widget):
        self.rows[f] = self.rows.get(f, 0)
        label = tk.Label(f, text=label_text, font=("Segoe UI", 10),
                         fg=FG, bg=BG)
        self._auto_wrap(label)
        label.grid(row=self.rows[f], column=0, sticky="w",
                   padx=self._px(18), pady=self._px(4))
        widget.grid(row=self.rows[f], column=1, sticky="e",
                    padx=self._px(18), pady=self._px(4))
        self.rows[f] += 1
        self._search_index.append(
            (label_text, self._frame_key[f], label, FG))

    def _full(self, f, widget):
        self.rows[f] = self.rows.get(f, 0)
        widget.grid(row=self.rows[f], column=0, columnspan=2, sticky="w",
                    padx=self._px(18), pady=self._px(4))
        self.rows[f] += 1

    def _note(self, f, text):
        self.rows[f] = self.rows.get(f, 0)
        tk.Label(f, text=text, font=("Segoe UI", 9), fg=DIM,
                 bg=BG, wraplength=self._px(470), justify="left").grid(
            row=self.rows[f], column=0, columnspan=2, sticky="w",
            padx=self._px(18), pady=(0, self._px(2)))
        self.rows[f] += 1

    def _scale(self, f, from_, to, resolution, var, command=None):
        return tk.Scale(f, from_=from_, to=to, resolution=resolution,
                        orient="horizontal", length=self._px(210),
                        width=self._px(14),
                        showvalue=True, variable=var, bg=FIELD_BG, fg=FG,
                        highlightthickness=0, troughcolor="#2A2F38",
                        font=("Segoe UI", 9), command=command)

    def _opt(self, f, var, values, width=None):
        m = tk.OptionMenu(f, var, *values)
        m.configure(bg=FIELD_BG, fg=FG, activebackground="#2A2F38",
                    activeforeground=FG, relief="flat",
                    highlightthickness=0, anchor="w" if not width else "e")
        m["menu"].configure(bg=FIELD_BG, fg=FG, activebackground="#2E86DE",
                            activeforeground="white", font=("Segoe UI", 10))
        if width:
            m.configure(width=width)
        return m


    def _build_record_tab(self, f):
        c = self.cfg
        self._pane_heading(f, "tabs.record", "settings.sub_record")
        # -- tab: recording --------------------------------------------------
        self._head(f, self.tr("settings.g_pauses"))
        self.v_silence = tk.IntVar(value=int(c.get("silence_ms", 2000)))
        self._row(f, self.tr("settings.silence"), self._scale(f, 500, 8000, 100,
                                                 self.v_silence))
        self._note(f, self.tr("settings.note_silence"))
        self.v_autostop = tk.BooleanVar(value=bool(c.get("auto_stop", True)))
        self._bind(f, self.v_autostop, self.tr("settings.autostop"))
        self.v_partial = tk.DoubleVar(value=float(c.get("partial_every_s", 1.5)))
        self._row(f, self.tr("settings.partial"), self._scale(f, 0.5, 5.0, 0.5,
                                                 self.v_partial))
        self._note(f, self.tr("settings.note_partial"))
        self.v_maxrec = tk.IntVar(value=int(c.get("max_record_s", 120)))
        self._row(f, self.tr("settings.maxrec"), self._scale(f, 30, 600, 30, self.v_maxrec))
        self.v_hold = tk.BooleanVar(value=bool(c.get("hold_to_talk", False)))
        self._bind(f, self.v_hold, self.tr("settings.hold_to_talk"))
        self._note(f, self.tr("settings.note_hold_to_talk"))
        self._head(f, self.tr("settings.g_sound"))
        self.v_beep_on = tk.BooleanVar(value=bool(c.get("beep_on", True)))
        self._bind(f, self.v_beep_on, self.tr("settings.beep_on"))
        self.v_beep_off = tk.BooleanVar(value=bool(c.get("beep_off", True)))
        self._bind(f, self.v_beep_off, self.tr("settings.beep_off"))
        self._head(f, self.tr("settings.g_text_rules"))
        self.v_text_rules = tk.BooleanVar(value=bool(c.get("text_rules", False)))
        self._bind(f, self.v_text_rules, self.tr("settings.text_rules"))
        self._note(f, self.tr("settings.note_text_rules"))


    def _build_model_tab(self, f):
        c = self.cfg
        self._pane_heading(f, "tabs.model", "settings.sub_model")
        # -- tab: model & language --------------------------------------------
        self._head(f, self.tr("model.g"))
        self.v_model = tk.StringVar(value=str(c.get("model", "large-v3-turbo")))
        self._row(f, self.tr("model.choose"), self._opt(f, self.v_model, MODELS, width=16))
        self.model_desc = tk.Label(f, text="", font=("Segoe UI", 9),
                                   fg=DIM, bg=BG, wraplength=self._px(400),
                                   justify="left")
        self.model_desc.grid(row=self.rows.get(f, 0), column=0, columnspan=2,
                             sticky="w", padx=self._px(16))
        self.rows[f] = self.rows.get(f, 0) + 1
        self._update_model_desc()
        self.v_model.trace_add("write", lambda *a: self._update_model_desc())
        self.v_keep = tk.BooleanVar(value=bool(c.get("model_keep_old", True)))
        self._bind(f, self.v_keep, self.tr("model.keep"))
        self._head(f, self.tr("settings.g_lang"))
        lang_pairs = [(LANG_NAMES.get(x, x), x) for x in LANGS]
        self._lang_by_name = dict(lang_pairs)
        cur_l = str(c.get("language", "en"))
        cur_l_disp = next((d for d, cd in lang_pairs if cd == cur_l), cur_l)
        self.v_lang = tk.StringVar(value=cur_l_disp)
        self._row(f, self.tr("settings.recog_lang"),
            self._opt(f, self.v_lang, [d for d, _ in lang_pairs], width=16))
        self._note(f, self.tr("settings.note_recog"))
        ui_pairs = [("auto", "auto")] + [
            (LANG_NAMES.get(x, x), x) for x in i18n.available_locales()]
        self._ui_by_name = dict(ui_pairs)
        cur_ui = str(c.get("locale", "auto"))
        cur_ui_disp = next((d for d, cd in ui_pairs if cd == cur_ui), cur_ui)
        self.v_ui = tk.StringVar(value=cur_ui_disp)
        self._row(f, self.tr("settings.ui_lang"),
            self._opt(f, self.v_ui, [d for d, _ in ui_pairs], width=16))
        self._note(f, self.tr("settings.note_ui"))
        self.model_status = tk.Label(f, text="", font=("Segoe UI", 9),
                                     fg="#9FE0A8", bg=BG, wraplength=self._px(420),
                                     justify="left", anchor="w")
        self._full(f, self.model_status)


    def _build_dictionary_tab(self, f):
        c = self.cfg
        self._pane_heading(f, "tabs.dictionary", "settings.sub_dictionary")
        # -- tab: dictionary -----------------------------------------------------
        self.dictionary = dictionary.normalize(c.get("dictionary"))
        self._head(f, self.tr("dictionary.g"))
        self._note(f, self.tr("dictionary.note"))
        self.dic_list = tk.Listbox(f, height=6, width=46, bg=FIELD_BG,
                                   fg=FG, relief="flat", highlightthickness=0,
                                   selectbackground="#2E86DE",
                                   font=("Segoe UI", 9))
        self._full(f, self.dic_list)
        self.v_dic_from = tk.StringVar(value="")
        self.v_dic_to = tk.StringVar(value="")
        self._row(f, self.tr("dictionary.from"),
            tk.Entry(f, textvariable=self.v_dic_from, width=26,
                     bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat"))
        self._row(f, self.tr("dictionary.to"),
            tk.Entry(f, textvariable=self.v_dic_to, width=26,
                     bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat"))
        dic_btns = tk.Frame(f, bg=BG)
        for text, command in (
                (self.tr("dictionary.add"), self._add_entry),
                (self.tr("dictionary.remove"), self._remove_entry),
                (self.tr("dictionary.import"), self._import_entries),
                (self.tr("dictionary.export"), self._export_entries)):
            tk.Button(dic_btns, text=text, command=command, bg="#22262E",
                      fg=FG, relief="flat", activebackground="#2A2F38",
                      activeforeground=FG, cursor="hand2").pack(
                          side="left", padx=(0, 6))
        self._full(f, dic_btns)
        preview_row = tk.Frame(f, bg=BG)
        tk.Button(preview_row, text=self.tr("dictionary.preview"),
                  command=self._preview, bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38", activeforeground=FG,
                  cursor="hand2").pack(side="left")
        self.v_dic_sample = tk.StringVar(value="")
        tk.Entry(preview_row, textvariable=self.v_dic_sample, width=30,
                 bg=FIELD_BG, fg=FG, insertbackground=FG,
                 relief="flat").pack(side="left", padx=(8, 0))
        self._full(f, preview_row)
        self._head(f, self.tr("dictionary.frequent"))
        self._note(f, self.tr("dictionary.frequent_note"))
        self.sug_list = tk.Listbox(f, height=4, width=46, bg=FIELD_BG,
                                   fg=FG, relief="flat", highlightthickness=0,
                                   selectbackground="#2E86DE",
                                   font=("Segoe UI", 9))
        self._full(f, self.sug_list)
        sug_btns = tk.Frame(f, bg=BG)
        tk.Button(sug_btns, text=self.tr("dictionary.frequent_add"),
                  command=self._add_suggestion, bg="#22262E", fg=FG,
                  relief="flat", activebackground="#2A2F38",
                  activeforeground=FG, cursor="hand2").pack(side="left")
        self._full(f, sug_btns)
        self.dic_status = tk.Label(f, text="", font=("Segoe UI", 9),
                                   fg="#9FE0A8", bg=BG, wraplength=self._px(420),
                                   justify="left", anchor="w")
        self._full(f, self.dic_status)
        self._refresh_dictionary()
        self._refresh_suggestions()


    def _build_history_tab(self, f):
        c = self.cfg
        title = tk.Label(f, text=self.tr("tabs.history"),
                         font=("Segoe UI", 15, "bold"), fg=FG, bg=BG)
        title.pack(anchor="w", padx=self._px(18),
                   pady=(self._px(18), self._px(2)))
        subtitle = tk.Label(f, text=self.tr("settings.sub_history"),
                            font=("Segoe UI", 9), fg=DIM, bg=BG)
        subtitle.pack(anchor="w", padx=self._px(18),
                      pady=(0, self._px(8)))
        self._search_index.append((self.tr("tabs.history"), "history",
                                   title, None))
        self._search_index.append((self.tr("settings.sub_history"), "history",
                                   subtitle, DIM))

        # -- bottom: history settings ---------------------------------------
        settings = tk.Frame(f, bg=FIELD_BG)
        settings.pack(side="bottom", fill="x", padx=self._px(18),
                      pady=(self._px(8), self._px(12)))
        tk.Frame(settings, bg=LINE, height=self._px(1)).pack(fill="x")
        self.v_history = tk.BooleanVar(value=bool(c.get("history", False)))
        self.v_history_app = tk.BooleanVar(
            value=bool(c.get("history_app", False)))
        for var, text in ((self.v_history, self.tr("history.enable")),
                          (self.v_history_app, self.tr("history.app"))):
            row = tk.Frame(settings, bg=FIELD_BG)
            row.pack(fill="x", padx=self._px(10), pady=(self._px(8), 0))
            label = tk.Label(row, text=text, font=("Segoe UI", 10), fg=FG,
                             bg=FIELD_BG, anchor="w", cursor="hand2",
                             justify="left")
            label.pack(side="left", fill="x", expand=True)
            toggle = _Toggle(row, var, self._px)
            toggle.pack(side="right")
            label.bind("<Button-1>", toggle.invoke)
            self._search_index.append((text, "history", label, FG))
        note = tk.Label(settings, text=self.tr("history.note",
                                               n=history.MAX_RECORDS),
                        font=("Segoe UI", 8), fg=DIM, bg=FIELD_BG,
                        anchor="w", justify="left")
        note.pack(fill="x", padx=self._px(10),
                  pady=(self._px(6), self._px(8)))
        self._auto_wrap(note, 760)

        # -- status line, above the settings strip ---------------------------
        self.his_status = tk.Label(f, text="", font=("Segoe UI", 9),
                                   fg="#9FE0A8", bg=BG, anchor="w",
                                   justify="left")
        self.his_status.pack(side="bottom", fill="x", padx=self._px(18),
                             pady=(0, self._px(2)))

        # -- top: search + refresh ------------------------------------------
        tools = tk.Frame(f, bg=BG)
        tools.pack(fill="x", padx=self._px(18), pady=(0, self._px(4)))
        tk.Label(tools, text=self.tr("history.search"), font=("Segoe UI", 9),
                 fg=DIM, bg=BG).pack(side="left")
        self.v_history_search = tk.StringVar(value="")
        self.his_search = tk.Entry(
            tools, textvariable=self.v_history_search, width=24,
            bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat",
            highlightthickness=1, highlightbackground="#3A4048",
            highlightcolor=ACCENT)
        self.his_search.pack(side="left", padx=(self._px(8), 0),
                             ipady=self._px(2))
        self.his_search.bind("<KeyRelease>",
                             lambda _e: self._refresh_history())
        refresh = tk.Button(
            tools, text=self.tr("history.refresh"),
            command=self._refresh_history, bg=FIELD_BG, fg=FG,
            relief="flat", activebackground="#2A2F38",
            activeforeground=FG, cursor="hand2", padx=self._px(8))
        refresh.pack(side="right")
        self._his_buttons = {"refresh": refresh}

        # -- filters: application, date, direction ---------------------------
        filters = tk.Frame(f, bg=BG)
        filters.pack(fill="x", padx=self._px(18), pady=(0, self._px(4)))
        self._his_filter_maps = {}
        for key, caption, width, choices in (
                ("app", "history.filter_app", 16,
                 [(self.tr("history.filter_all"), "")]),
                ("date", "history.filter_date", 12,
                 [(self.tr("history.filter_all"), 0),
                  (self.tr("history.filter_today"), 1),
                  (self.tr("history.filter_7"), 7),
                  (self.tr("history.filter_30"), 30)]),
                ("way", "history.filter_way", 13,
                 [(self.tr("history.filter_all"), ""),
                  (self.tr("history.filter_insert"), "insert"),
                  (self.tr("history.filter_copy"), "copy")])):
            tk.Label(filters, text=self.tr(caption), font=("Segoe UI", 9),
                     fg=DIM, bg=BG).pack(side="left")
            combo = ttk.Combobox(
                filters, state="readonly", width=width,
                values=[label for label, _value in choices],
                style="Vibe.TCombobox")
            combo.set(choices[0][0])
            combo.pack(side="left", padx=(self._px(6), self._px(14)),
                       ipady=self._px(1))
            combo.bind("<<ComboboxSelected>>",
                       lambda _e: self._refresh_history())
            self._his_filter_maps[key] = (combo, dict(choices))

        # -- actions ---------------------------------------------------------
        actions = tk.Frame(f, bg=BG)
        actions.pack(fill="x", padx=self._px(18), pady=(0, self._px(2)))
        for key, text, command in (
                ("copy", "history.copy", self._copy_selected),
                ("paste", "history.paste_again", self._paste_again_selected),
                ("dict", "history.to_dictionary", self._pair_from_selected),
                ("delete", "history.delete", self._delete_selected),
                ("clear", "history.clear", self._clear_history)):
            button = tk.Button(actions, text=self.tr(text), command=command,
                               bg="#22262E", fg=FG, relief="flat",
                               activebackground="#2A2F38",
                               activeforeground=FG, cursor="hand2",
                               padx=self._px(6))
            button.pack(side="left", padx=(0, self._px(6)))
            self._his_buttons[key] = button
        for key, text, command in (
                ("undo", "history.undo", self._undo_last),
                ("restore", "history.restore", self._restore_clipboard)):
            button = tk.Button(actions, text=self.tr(text), command=command,
                               bg="#22262E", fg=FG, relief="flat",
                               activebackground="#2A2F38",
                               activeforeground=FG, cursor="hand2",
                               padx=self._px(6))
            button.pack(side="right", padx=(self._px(6), 0))
            self._his_buttons[key] = button

        # -- table -----------------------------------------------------------
        table_host = tk.Frame(f, bg=BG)
        table_host.pack(fill="both", expand=True, padx=self._px(18),
                        pady=(self._px(4), 0))
        columns = ("when", "chars", "app", "text")
        self.his_tree = ttk.Treeview(
            table_host, columns=columns, show="headings", height=16,
            style="Vibe.Treeview", selectmode="extended")
        for column, key, width, anchor in (
                ("when", "history.col_when", 120, "w"),
                ("chars", "history.col_chars", 55, "e"),
                ("app", "history.col_app", 130, "w"),
                ("text", "history.col_text", 420, "w")):
            self.his_tree.heading(column, text=self.tr(key), anchor=anchor)
            self.his_tree.column(
                column, width=self._px(width), minwidth=self._px(45),
                stretch=(column == "text"), anchor=anchor)
        self.his_tree.tag_configure("odd", background=BG, foreground=FG)
        self.his_tree.tag_configure("even", background="#1B1E24",
                                    foreground=FG)
        bar = ttk.Scrollbar(table_host, orient="vertical",
                            style="Vibe.Vertical.TScrollbar",
                            command=self.his_tree.yview)
        self.his_tree.configure(yscrollcommand=bar.set)
        self.his_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.his_tree.bind("<<TreeviewSelect>>",
                           lambda _e: self._sync_history_actions())
        self.his_tree.bind("<Delete>", lambda _e: self._delete_selected())
        self.his_tree.bind("<Control-a>", self._select_all_history)
        self.his_tree.bind("<Double-1>", lambda _e: self._copy_selected())
        self.his_tree.bind("<MouseWheel>", self._wheel_history)

        self.v_history.trace_add("write",
                                 lambda *_a: self._refresh_history())
        self._history_items = []
        self._refresh_history()


    def _build_look_tab(self, f):
        c = self.cfg
        self._pane_heading(f, "tabs.look", "settings.sub_look")
        # -- tab: appearance ---------------------------------------------------
        self._head(f, self.tr("settings.g_panel"))
        self.v_alpha = tk.DoubleVar(value=float(c.get("hud_alpha", 0.94)))
        self._row(f, self.tr("settings.alpha"),
            self._scale(f, 0.4, 1.0, 0.05, self.v_alpha,
                  command=lambda v: self.svc.live.__setitem__("alpha", float(v))))
        self._note(f, self.tr("settings.note_alpha"))
        self.v_autogrow = tk.BooleanVar(value=bool(c.get("hud_autogrow", True)))
        self._bind(f, self.v_autogrow, self.tr("settings.autogrow"))
        self.v_hud = tk.BooleanVar(value=bool(c.get("hud", True)))
        self._bind(f, self.v_hud, self.tr("settings.show_panel"))


    def _build_system_tab(self, f):
        c = self.cfg
        self._pane_heading(f, "tabs.system", "settings.sub_system")
        # -- tab: system -------------------------------------------------------
        self._head(f, self.tr("settings.g_paste"))
        self.v_paste = tk.StringVar(value=c.get("paste_method", "shift_insert"))
        self._row(f, self.tr("settings.paste"),
            self._opt(f, self.v_paste, ("shift_insert", "ctrl_v"), width=16))
        self.v_restore = tk.BooleanVar(value=bool(c.get("restore_clipboard", True)))
        self._bind(f, self.v_restore, self.tr("settings.restore"))
        self.v_confirm = tk.BooleanVar(
            value=bool(c.get("confirm_focus_change", True)))
        self._bind(f, self.v_confirm, self.tr("settings.confirm_focus"))
        self.v_exclude = tk.BooleanVar(
            value=bool(c.get("clipboard_exclude_history", True)))
        self._bind(f, self.v_exclude, self.tr("settings.exclude_history"))
        self._note(f, self.tr("settings.note_exclude_history"))
        self.v_notify_text = tk.BooleanVar(
            value=bool(c.get("notify_text", False)))
        self._bind(f, self.v_notify_text, self.tr("settings.notify_text"))
        self._head(f, self.tr("sys.g_startup"))
        self.v_hotkey = tk.StringVar(value=c.get("hotkey", "ctrl+alt+space"))
        self._row(f, self.tr("settings.hotkey"),
            tk.Entry(f, textvariable=self.v_hotkey, width=18,
                     bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat"))
        self._note(f, self.tr("settings.note_hotkey"))
        names = self.svc.input_device_names() or []
        default_label = self.tr("settings.device_default")
        self._device_default = default_label
        self._device_by_name = {default_label: ""}
        self._device_by_name.update({name: name for name in names})
        current = str(c.get("input_device", ""))
        shown = next((disp for disp, val in self._device_by_name.items()
                      if val == current), default_label)
        self.v_device = tk.StringVar(value=shown)
        self._row(f, self.tr("settings.device"),
            self._opt(f, self.v_device, list(self._device_by_name), width=24))
        self.v_autostart = tk.BooleanVar(value=bool(c.get("autostart", True)))
        self._bind(f, self.v_autostart, self.tr("settings.autostart_w"))
        btn_setup = tk.Button(f, text=self.tr("settings.onboarding"),
                              command=self.svc.open_onboarding, width=22,
                              bg="#22262E", fg=FG, relief="flat",
                              activebackground="#2A2F38", cursor="hand2")
        self._full(f, btn_setup)
        btn_diag = tk.Button(f, text=self.tr("doctor.button"),
                             command=self._open_doctor, width=22,
                             bg="#22262E", fg=FG, relief="flat",
                             activebackground="#2A2F38", cursor="hand2")
        self._full(f, btn_diag)
        self._head(f, self.tr("settings.g_voice_forms"))
        self.v_http = tk.BooleanVar(value=bool(c.get("http_api", False)))
        self._bind(f, self.v_http, self.tr("settings.http_api"))
        api_port = int(c.get("http_port", 8787))
        server = getattr(self.svc, "http_server", None)
        if server is not None:
            try:
                api_port = int(server.server_address[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                pass
        self._note(f, self.tr("settings.note_http_api", port=api_port))
        self._head(f, self.tr("settings.g_hook"))
        self.v_hook = tk.StringVar(value=str(c.get("hook_command", "")))
        self._row(f, self.tr("settings.hook_command"),
            tk.Entry(f, textvariable=self.v_hook, width=24,
                     bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat"))
        self.v_hook_timeout = tk.IntVar(
            value=int(c.get("hook_timeout_s", 5)))
        self._row(f, self.tr("settings.hook_timeout"),
                  self._scale(f, 1, 30, 1, self.v_hook_timeout))
        self._note(f, self.tr("settings.note_hook"))
        self._head(f, self.tr("updates.g"))
        self.v_upd = tk.BooleanVar(value=bool(c.get("update_check", True)))
        self._bind(f, self.v_upd, self.tr("updates.auto"))
        self.v_upd_auto = tk.BooleanVar(
            value=c.get("auto_update") == "auto")
        self._bind(f, self.v_upd_auto, self.tr("updates.install_auto"))
        self.v_upd_h = tk.IntVar(value=int(c.get("update_interval_h", 2)))
        self._row(f, self.tr("updates.interval"), self._scale(f, 1, 24, 1, self.v_upd_h))
        btn_upd = tk.Button(f, text=self.tr("updates.check_now"),
                            command=self.svc.check_updates_now, width=16,
                            bg="#22262E", fg=FG, relief="flat",
                            activebackground="#2A2F38", activeforeground=FG,
                            cursor="hand2")
        self._full(f, btn_upd)
        self.ram_lbl = tk.Label(f, text="", font=("Segoe UI", 9),
                                fg="#9FE0A8", bg=BG, anchor="w")
        self.ram_lbl.grid(row=self.rows[f], column=0, columnspan=2,
                          sticky="w", padx=self._px(16))
        self.rows[f] += 1
        self._refresh_ram()


    def _build_footer(self):
        # Packed before the expanding content: on a short window (150%
        # scaling) the sections shrink and the buttons stay visible.
        foot = self._foot = tk.Frame(self.win, bg=BG)
        foot.pack(side="bottom", fill="x", before=self._main)
        tk.Frame(foot, bg=LINE, height=self._px(1)).pack(
            fill="x", padx=self._px(18), pady=(self._px(8), 0))
        btns = tk.Frame(foot, bg=BG)
        btns.pack(fill="x", padx=self._px(18),
                  pady=(self._px(8), self._px(12)))
        tk.Button(btns, text=self.tr("settings.save"), command=self._save,
                  width=11, bg=ACCENT, fg="white", relief="flat",
                  activebackground="#1B6EC2", activeforeground="white",
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  pady=self._px(4)).pack(side="right")
        tk.Button(btns, text=self.tr("settings.cancel"),
                  command=self.win.destroy, width=10, bg=FIELD_BG, fg=FG,
                  relief="flat", activebackground="#2A2F38",
                  activeforeground=FG, font=("Segoe UI", 10),
                  cursor="hand2", pady=self._px(4)).pack(
                      side="right", padx=(0, self._px(8)))
        tk.Button(btns, text=self.tr("settings.reset"), command=self._reset,
                  width=10, bg=FIELD_BG, fg=FG, relief="flat",
                  activebackground="#2A2F38", activeforeground=FG,
                  font=("Segoe UI", 10), cursor="hand2",
                  pady=self._px(4)).pack(side="left")
        tk.Label(btns, text=self.tr("settings.version", ver=VERSION),
                 font=("Segoe UI", 9), fg=DIM,
                 bg=BG).pack(side="left", padx=(self._px(12), 0))
        self.save_status = tk.Label(btns, text="", font=("Segoe UI", 9),
                                    fg="#9FE0A8", bg=BG)
        self.save_status.pack(side="left", padx=(self._px(12), 0))

    def _fit_to_content(self, frames):
        """Size to the tallest section once, centred and clamped to the screen."""
        self.win.update_idletasks()
        values = tuple(frames[key] for key in
                       ("record", "model", "dictionary", "snippets",
                        "history", "stats", "look", "system"))
        w = max(self._px(1060),
                self._px(214 + 1) + max(f.winfo_reqwidth() for f in values)
                + self._px(72))
        top = (self._header.winfo_reqheight()
               + self._search_row.winfo_reqheight())
        h = (max(f.winfo_reqheight() for f in values)
             + top + self._foot.winfo_reqheight() + self._px(24))
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = window_layout.fit_size(w, h, sw, sh, dpi_factor(self.win))
        x, y = window_layout.place_xy(w, h, sw, sh)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _bind(self, f, var, text):
        """Switch bound to var, placed in frame f; the label is clickable."""
        rows = self.rows
        rows[f] = rows.get(f, 0)
        label = tk.Label(f, text=text, justify="left", font=("Segoe UI", 10),
                         fg=FG, bg=BG, anchor="w", cursor="hand2")
        self._auto_wrap(label)
        label.grid(row=rows[f], column=0, sticky="w", padx=self._px(18),
                   pady=self._px(4))
        toggle = _Toggle(f, var, self._px)
        toggle.grid(row=rows[f], column=1, sticky="e", padx=self._px(18),
                    pady=self._px(4))
        label.bind("<Button-1>", toggle.invoke)
        rows[f] += 1
        self._search_index.append(
            (text, self._frame_key[f], label, FG))
        return var

    def select_tab(self, key):
        """Jump to a section by key (record/model/dictionary/history/look/system)."""
        self._show(key)

    # -- search, reset, inline model progress --------------------------------
    def _focus_search(self):
        """Ctrl+F: the table search on History, the settings search elsewhere."""
        if self._shown == "history" and hasattr(self, "his_search"):
            self.his_search.focus_set()
        else:
            self.search_entry.focus_set()

    def _search(self):
        """Jump to the first setting whose label contains the query."""
        query = self.v_search.get().strip().casefold()
        if not query:
            return
        for text, key, label, fg in self._search_index:
            if query in text.casefold():
                self._show(key)
                if label is not None:
                    self._flash(label, fg)
                return
        self.save_status.configure(text=self.tr("settings.search_nothing"))

    def _flash(self, label, fg):
        """Highlight a found setting for a moment, then restore its colors."""
        try:
            original_bg = label.cget("bg")
            label.configure(bg=ACCENT, fg="white")

            def _restore():
                if self.win.winfo_exists():
                    label.configure(bg=original_bg, fg=fg)
            self.win.after(1500, _restore)
        except tk.TclError:
            pass

    def _reset(self):
        """Restore option defaults; the model, dictionary and history stay."""
        d = DEFAULTS
        self.v_silence.set(d["silence_ms"])
        self.v_partial.set(d["partial_every_s"])
        self.v_maxrec.set(d["max_record_s"])
        self.v_hold.set(d["hold_to_talk"])
        self.v_autostop.set(d["auto_stop"])
        self.v_beep_on.set(d["beep_on"])
        self.v_beep_off.set(d["beep_off"])
        self.v_keep.set(d["model_keep_old"])
        self.v_lang.set(LANG_NAMES.get(d["language"], d["language"]))
        self.v_ui.set("auto")
        self.v_paste.set(d["paste_method"])
        self.v_restore.set(d["restore_clipboard"])
        self.v_confirm.set(d["confirm_focus_change"])
        self.v_exclude.set(d["clipboard_exclude_history"])
        self.v_notify_text.set(d["notify_text"])
        self.v_hud.set(d["hud"])
        self.v_alpha.set(d["hud_alpha"])
        self.v_autogrow.set(d["hud_autogrow"])
        self.v_hotkey.set(d["hotkey"])
        self.v_device.set(self._device_default)
        self.v_autostart.set(d["autostart"])
        self.v_upd.set(d["update_check"])
        self.v_upd_auto.set(d["auto_update"] == "auto")
        self.v_upd_h.set(d["update_interval_h"])
        self.v_hook.set(d["hook_command"])
        self.v_hook_timeout.set(d["hook_timeout_s"])
        self.snippets_tab.reset()
        self.save_status.configure(text=self.tr("settings.reset_done"))

    def _poll_model(self, progress):
        """Show the switch progress inside the settings window."""
        try:
            if not self.win.winfo_exists():
                return
            p = dict(progress)
            self.model_status.configure(text=phase_text(self.tr, p))
            if p.get("phase") not in ("done", "error", "busy"):
                self.win.after(400, lambda: self._poll_model(progress))
        except tk.TclError:
            pass

    # -- dictionary tab ------------------------------------------------------
    def _refresh_dictionary(self):
        self.dic_list.delete(0, "end")
        for entry in self.dictionary:
            line = entry["from"]
            if entry["to"]:
                line += "  ->  " + entry["to"]
            self.dic_list.insert("end", line)

    def _add_entry(self):
        entry = dictionary.normalize([{
            "from": self.v_dic_from.get(),
            "to": self.v_dic_to.get(),
        }])
        if not entry:
            self.dic_status.configure(text=self.tr("dictionary.no_from"))
            return
        self.dictionary = dictionary.normalize(self.dictionary + entry)
        self.v_dic_from.set("")
        self.v_dic_to.set("")
        self._refresh_dictionary()
        self.dic_status.configure(text="")

    def _refresh_suggestions(self):
        """Repeated draft corrections, minus the pairs already in the list."""
        if not hasattr(self, "sug_list"):
            return
        self.sug_list.delete(0, "end")
        self._suggestions = []
        known = {entry["from"].casefold() for entry in self.dictionary}
        for row in dictlearn.suggestions():
            if row["from"].casefold() in known:
                continue
            self._suggestions.append(row)
            self.sug_list.insert("end", self.tr(
                "dictionary.frequent_row", src=row["from"],
                dst=row["to"], n=row["count"]))

    def _add_suggestion(self):
        """Add a frequent correction to the dictionary - by confirmation only."""
        selected = self.sug_list.curselection() if hasattr(
            self, "sug_list") else ()
        if not selected:
            self.dic_status.configure(
                text=self.tr("dictionary.frequent_select"))
            return
        index = int(selected[0])
        if not 0 <= index < len(self._suggestions):
            return
        row = self._suggestions[index]
        if not dictlearn.add_pair(self.cfg, None, row["from"], row["to"]):
            return
        self.dictionary = dictionary.normalize(self.cfg.get("dictionary"))
        self._refresh_dictionary()
        self._refresh_suggestions()
        self.dic_status.configure(text=self.tr(
            "dictionary.frequent_added", src=row["from"], dst=row["to"]))

    def _remove_entry(self):
        selected = self.dic_list.curselection()
        if not selected:
            return
        index = int(selected[0])
        if 0 <= index < len(self.dictionary):
            del self.dictionary[index]
            self._refresh_dictionary()

    def _import_entries(self):
        path = filedialog.askopenfilename(
            parent=self.win, title=self.tr("dictionary.import"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                imported = dictionary.from_json(f.read())
        except (OSError, ValueError) as e:
            self.log.exception("Dictionary import failed: %s", e)
            self.dic_status.configure(text=self.tr("dictionary.bad_file"))
            return
        self.dictionary = dictionary.normalize(self.dictionary + imported)
        self._refresh_dictionary()
        self.dic_status.configure(
            text=self.tr("dictionary.imported", n=len(imported)))

    def _export_entries(self):
        path = filedialog.asksaveasfilename(
            parent=self.win, title=self.tr("dictionary.export"),
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(dictionary.to_json(self.dictionary))
        except OSError as e:
            self.log.exception("Dictionary export failed: %s", e)
            self.dic_status.configure(text=self.tr("dictionary.bad_file"))
            return
        self.dic_status.configure(
            text=self.tr("dictionary.exported", path=os.path.basename(path)))

    def _preview(self):
        sample = self.v_dic_sample.get()
        if not sample:
            self.dic_status.configure(text=self.tr("dictionary.preview_none"))
            return
        result, count = dictionary.apply(sample, self.dictionary)
        if count:
            text = self.tr("dictionary.preview_result", text=result,
                           n=count)
        else:
            text = self.tr("dictionary.preview_same", text=result)
        self.dic_status.configure(text=text)

    # -- history tab ---------------------------------------------------------
    def _history_filter_value(self, key):
        """The filter's current value: the label maps to it, never a string."""
        combo, values = self._his_filter_maps[key]
        return values.get(combo.get())

    def _sync_history_app_filter(self, items):
        """Keep the application choices in step with the stored entries."""
        combo, _values = self._his_filter_maps["app"]
        all_label = self.tr("history.filter_all")
        names = sorted({str(record.get("app") or "") for record in items
                        if record.get("app")})
        choices = {all_label: ""}
        for name in names:
            choices.setdefault(name, name)
        current = combo.get()
        combo.configure(values=list(choices))
        self._his_filter_maps["app"] = (combo, choices)
        combo.set(current if current in choices else all_label)

    def _refresh_history(self):
        try:
            items = list(self.svc.history_records())
        except Exception as e:  # noqa: BLE001 - a broken file shows as empty
            self.log.warning("History read failed: %s", e)
            items = []
        self._history_items = items
        self._sync_history_app_filter(items)
        rows = self.his_tree.get_children()
        if rows:
            self.his_tree.delete(*rows)
        if not self.v_history.get():
            self.his_status.configure(text=self.tr("history.disabled"))
            self._sync_history_actions()
            return
        query = self.v_history_search.get().strip()
        app = str(self._history_filter_value("app") or "")
        try:
            days = int(self._history_filter_value("date") or 0)
        except (TypeError, ValueError):
            days = 0
        way = str(self._history_filter_value("way") or "")
        matching = history.records_matching(items, query=query, app=app,
                                            days=days, way=way)
        keep = {id(record) for record in matching}
        shown = 0
        for index in range(len(items) - 1, -1, -1):
            record = items[index]
            if id(record) not in keep:
                continue
            stamp, chars, app_name, text = history_row(record)
            self.his_tree.insert(
                "", "end", iid=str(index),
                values=(stamp, chars, app_name or "—", text),
                tags=("odd" if shown % 2 == 0 else "even",))
            shown += 1
        if query or app or days or way:
            key = "history.found" if shown else "history.no_match"
            self.his_status.configure(
                text=self.tr(key, n=shown, total=len(items)))
        elif not items:
            self.his_status.configure(text=self.tr("history.empty"))
        else:
            self.his_status.configure(text=self.tr("history.count",
                                                   n=len(items)))
        self._sync_history_actions()

    def _sync_history_actions(self):
        has_rows = bool(self.his_tree.get_children())
        has_selection = bool(self.his_tree.selection())
        for key in ("copy", "paste", "dict", "delete"):
            self._his_buttons[key].configure(
                state="normal" if has_selection else "disabled")
        self._his_buttons["clear"].configure(
            state="normal" if has_rows and self.v_history.get()
            else "disabled")

    def _selected_history(self):
        selected = self.his_tree.selection()
        if not selected:
            self.his_status.configure(text=self.tr("history.select_first"))
            return None
        with contextlib.suppress(ValueError, TypeError):
            index = int(selected[0])
            if 0 <= index < len(self._history_items):
                return index, self._history_items[index]
        return None

    def _selected_indexes(self):
        indexes = []
        for iid in self.his_tree.selection():
            with contextlib.suppress(ValueError, TypeError):
                index = int(iid)
                if 0 <= index < len(self._history_items):
                    indexes.append(index)
        return indexes

    def _copy_selected(self):
        picked = self._selected_history()
        if not picked:
            return
        self.svc.history_copy(str(picked[1].get("text") or ""))
        self.his_status.configure(text=self.tr("history.copied"))

    def _paste_again_selected(self):
        picked = self._selected_history()
        if not picked:
            return
        self.svc.history_copy(str(picked[1].get("text") or ""),
                              hint="history.paste_again_hint")
        self.win.destroy()

    def _pair_from_selected(self):
        """Offer the selected entry as a dictionary pair; the entry stays."""
        picked = self._selected_history()
        if not picked:
            return
        added = dictlearn.pair_dialog(
            self.win, self.tr, str(picked[1].get("text") or ""), self.cfg)
        if not added:
            return
        self.dictionary = dictionary.normalize(self.cfg.get("dictionary"))
        self._refresh_dictionary()
        self._refresh_suggestions()
        self.his_status.configure(text=self.tr("history.dictionary_added"))

    def _delete_selected(self):
        indexes = self._selected_indexes()
        if not indexes:
            self.his_status.configure(text=self.tr("history.select_first"))
            return
        for index in sorted(indexes, reverse=True):
            self.svc.history_delete(index)
        self._refresh_history()
        text = (self.tr("history.deleted") if len(indexes) == 1
                else self.tr("history.deleted_many", n=len(indexes)))
        self.his_status.configure(text=text)

    def _clear_history(self):
        if not messagebox.askyesno(self.tr("history.clear"),
                                   self.tr("history.clear_question"),
                                   parent=self.win):
            return
        self.svc.history_clear()
        self._refresh_history()
        self.his_status.configure(text=self.tr("history.cleared"))

    def _select_all_history(self, _event=None):
        rows = self.his_tree.get_children()
        if rows:
            self.his_tree.selection_set(rows)
        return "break"

    def _wheel_history(self, event):
        self.his_tree.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _undo_last(self):
        self.his_status.configure(text=self.svc.undo_last())

    def _restore_clipboard(self):
        self.his_status.configure(text=self.svc.restore_last_clipboard())

    def _update_model_desc(self):
        with contextlib.suppress(Exception):
            self.model_desc.configure(
                text=self.tr(f"models.desc.{self.v_model.get()}"))

    def _open_doctor(self):
        import doctor
        doctor.show_report(self.win, self.tr, self.log)

    def _refresh_ram(self):
        try:
            if not self.win.winfo_exists():
                return
            mb = process_ram_mb()
            self.ram_lbl.configure(text=self.tr("settings.ram", mb=mb))
        except Exception as e:  # noqa: BLE001 - the window must survive any Tk failure
            self.ram_lbl.configure(text=f"RAM: {e}")
        self.win.after(1500, self._refresh_ram)

    def _set_snippets(self, entries):
        """The Snippets tab reports its list; Save writes it to config."""
        self.snippets = entries

    def _save(self):
        c = dict(self.cfg)
        old_history = bool(self.cfg.get("history", False))
        c["silence_ms"] = int(self.v_silence.get())
        c["partial_every_s"] = float(self.v_partial.get())
        c["max_record_s"] = int(self.v_maxrec.get())
        c["auto_stop"] = bool(self.v_autostop.get())
        c["beep_on"] = bool(self.v_beep_on.get())
        c["beep_off"] = bool(self.v_beep_off.get())
        c["model_keep_old"] = bool(self.v_keep.get())
        c["language"] = self._lang_by_name.get(
            self.v_lang.get(), self.cfg.get("language", "en"))
        c["locale"] = self._ui_by_name.get(
            self.v_ui.get(), self.cfg.get("locale", "auto"))
        c["paste_method"] = self.v_paste.get()
        c["restore_clipboard"] = bool(self.v_restore.get())
        c["confirm_focus_change"] = bool(self.v_confirm.get())
        c["clipboard_exclude_history"] = bool(self.v_exclude.get())
        c["notify_text"] = bool(self.v_notify_text.get())
        c["dictionary"] = dictionary.normalize(self.dictionary)
        c["snippets"] = snippets.normalize(self.snippets)
        c["text_rules"] = bool(self.v_text_rules.get())
        c["hold_to_talk"] = bool(self.v_hold.get())
        c["history"] = bool(self.v_history.get())
        c["history_app"] = bool(self.v_history_app.get())
        c["hud"] = bool(self.v_hud.get())
        c["hud_alpha"] = float(self.v_alpha.get())
        c["hud_autogrow"] = bool(self.v_autogrow.get())
        hotkey = onboarding.normalize_hotkey(
            self.v_hotkey.get().strip() or c.get("hotkey", "ctrl+alt+space"))
        ok, code, _warning = onboarding.validate_hotkey(hotkey)
        if not ok:
            from tkinter import messagebox
            messagebox.showwarning(self.tr("settings.hotkey"),
                                   self.tr("onboarding.hotkey_" + code),
                                   parent=self.win)
            return
        c["hotkey"] = hotkey
        c["input_device"] = self._device_by_name.get(
            self.v_device.get(), self.v_device.get()).strip()
        c["autostart"] = bool(self.v_autostart.get())
        c["http_api"] = bool(self.v_http.get())
        c["hook_command"] = self.v_hook.get().strip()
        c["hook_timeout_s"] = int(self.v_hook_timeout.get())
        c["update_check"] = bool(self.v_upd.get())
        c["update_interval_h"] = int(self.v_upd_h.get())
        c["auto_update"] = "auto" if self.v_upd_auto.get() else "notify"
        new_model = self.v_model.get()
        model_changed = new_model != c.get("model")
        if not model_changed:
            c["model"] = self.cfg["model"]   # unchanged — write as is
        else:
            c.pop("model", None)   # the switch flow owns it; revert stays safe
        # when changed: c carries no "model" key — the switch flow owns it
        try:
            self.svc.apply_settings(c)
        except Exception as e:
            self.log.exception("Saving settings failed: %s", e)
            self.save_status.configure(text=self.tr("settings.save_failed"))
            return
        if old_history and not c["history"]:
            # Turning the history off forgets what was stored locally.
            self.svc.history_clear()
        # The window stays open: the confirmation is visible right here.
        self.save_status.configure(text=self.tr("settings.saved"))
        if model_changed:
            progress = {"phase": "download", "name": new_model}
            self.model_status.configure(text=phase_text(self.tr, progress))
            self._poll_model(progress)
            self.svc.switch_model(new_model, bool(self.v_keep.get()), progress)
