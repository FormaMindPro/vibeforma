"""Settings dialog (tkinter Toplevel) — lives on the HUD main thread.

Localized through i18n (en/ru shipped). Organized into tabs (ttk.Notebook):
Recording / Model & language / Dictionary / History / Appearance / System —
the window stays compact no matter how many settings land. The model tab
switches models through a progress dialog (download -> load -> remove old
on request). Dictionary and history are local: no cloud, no account.
"""
import contextlib
import ctypes
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dictionary
import history
import i18n
import onboarding
import window_layout
from config import DEFAULTS, LANG_NAMES, LANGS, MODEL_GB, MODELS, VERSION

BG = "#16181D"
FG = "#D5D9E0"
DIM = "#9AA3B0"
FIELD_BG = "#22262E"


def dpi_factor(win):
    """Windows scaling factor (1.0 at 100%, 1.5 at 150%) for fixed-pixel sizes."""
    try:
        return max(1.0, win.winfo_fpixels("1i") / 96.0)
    except Exception:  # noqa: BLE001 - the window must survive any Tk failure
        return 1.0

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

        self._body = tk.Frame(self.win, bg=BG)
        self._body.pack(fill="both", expand=True)

        self._style_notebook()
        nb = self.nb = ttk.Notebook(self._body, style="Vibe.TNotebook")
        nb.pack(fill="both", expand=True)
        self._build_search()

        frames = self._create_tab_frames()
        self.rows = {}   # shared by the row helpers below
        self._build_record_tab(frames["record"])
        self._build_model_tab(frames["model"])
        self._build_dictionary_tab(frames["dictionary"])
        self._build_history_tab(frames["history"])
        self._build_look_tab(frames["look"])
        self._build_system_tab(frames["system"])
        self._build_footer()
        self._body.columnconfigure(0, weight=1)
        if tab in self._tab_frames:
            nb.select(self._tab_frames[tab])
        self._fit_to_content(frames)

    # -- construction steps ------------------------------------------------
    def _style_notebook(self):
        style = ttk.Style(self.win)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        style.configure("Vibe.TNotebook", background=BG, borderwidth=0,
                        tabmargins=(self._px(8), self._px(4), self._px(8), 0))
        # clam ignores a static foreground on tabs - drive ALL text colors
        # through the state map (static fg renders as near-invisible dark)
        style.configure("Vibe.TNotebook.Tab", background=FIELD_BG,
                        padding=(self._px(10), self._px(4)),
                        font=("Segoe UI", 10))
        style.map("Vibe.TNotebook.Tab",
                  background=[("selected", "#2E86DE")],
                  foreground=[("selected", "white"), ("!selected", FG)])
        # clam draws the scrollbar from these elements; without the dark
        # border/light colors it falls back to a light native-looking track
        style.configure("Vibe.Vertical.TScrollbar", troughcolor=BG,
                        background="#3A4048", bordercolor=BG,
                        darkcolor=BG, lightcolor=BG, arrowcolor=DIM,
                        relief="flat", arrowsize=self._px(10))
        style.map("Vibe.Vertical.TScrollbar",
                  background=[("active", "#4A515C")])

    def _build_search(self):
        search_row = tk.Frame(self._body, bg=BG)
        search_row.pack(fill="x", padx=self._px(16), pady=(self._px(6), 0),
                        before=self.nb)
        tk.Label(search_row, text=self.tr("settings.search"),
                 font=("Segoe UI", 9), fg=DIM, bg=BG).pack(side="left")
        self.v_search = tk.StringVar(value="")
        search_entry = tk.Entry(search_row, textvariable=self.v_search,
                                width=24, bg=FIELD_BG, fg=FG,
                                insertbackground=FG, relief="flat",
                                highlightthickness=1,
                                highlightbackground="#3A4048",
                                highlightcolor="#2E86DE")
        search_entry.pack(side="left", padx=(self._px(8), 0))
        search_entry.bind("<Return>", lambda _e: self._search())

    def _create_tab_frames(self):
        self._search_index = []   # (label text, tab key, label widget)
        frames = {}
        self._scrolls = {}
        for key, title_key in (
                ("record", "tabs.record"), ("model", "tabs.model"),
                ("dictionary", "tabs.dictionary"), ("history", "tabs.history"),
                ("look", "tabs.look"), ("system", "tabs.system")):
            pane = _ScrollPane(self.nb, self._px)
            self.nb.add(pane.host, text=self.tr(title_key))
            self._scrolls[key] = pane
            frames[key] = pane.inner
        self._tab_frames = {key: p.host for key, p in self._scrolls.items()}
        self._frame_key = {p.inner: key for key, p in self._scrolls.items()}
        return frames

    # -- row helpers -------------------------------------------------------
    def _head(self, f, text):
        self.rows[f] = self.rows.get(f, 0)
        label = tk.Label(f, text=text, font=("Segoe UI", 10, "bold"),
                         fg="#8FB8E8", bg=BG)
        label.grid(row=self.rows[f], column=0, columnspan=2, sticky="w",
                   padx=self._px(16), pady=(self._px(12), self._px(2)))
        self.rows[f] += 1
        self._search_index.append((text, self._frame_key[f], label))

    def _row(self, f, label_text, widget):
        self.rows[f] = self.rows.get(f, 0)
        label = tk.Label(f, text=label_text, font=("Segoe UI", 10),
                         fg=FG, bg=BG)
        label.grid(row=self.rows[f], column=0, sticky="w",
                   padx=self._px(16), pady=self._px(2))
        widget.grid(row=self.rows[f], column=1, sticky="e",
                    padx=self._px(16), pady=self._px(2))
        self.rows[f] += 1
        self._search_index.append(
            (label_text, self._frame_key[f], label))

    def _full(self, f, widget):
        self.rows[f] = self.rows.get(f, 0)
        widget.grid(row=self.rows[f], column=0, columnspan=2, sticky="w",
                    padx=self._px(16), pady=self._px(2))
        self.rows[f] += 1

    def _note(self, f, text):
        self.rows[f] = self.rows.get(f, 0)
        tk.Label(f, text=text, font=("Segoe UI", 9), fg=DIM,
                 bg=BG, wraplength=self._px(400), justify="left").grid(
            row=self.rows[f], column=0, columnspan=2, sticky="w",
            padx=self._px(16))
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
        self._head(f, self.tr("settings.g_sound"))
        self.v_beep_on = tk.BooleanVar(value=bool(c.get("beep_on", True)))
        self._bind(f, self.v_beep_on, self.tr("settings.beep_on"))
        self.v_beep_off = tk.BooleanVar(value=bool(c.get("beep_off", True)))
        self._bind(f, self.v_beep_off, self.tr("settings.beep_off"))


    def _build_model_tab(self, f):
        c = self.cfg
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
        self.dic_status = tk.Label(f, text="", font=("Segoe UI", 9),
                                   fg="#9FE0A8", bg=BG, wraplength=self._px(420),
                                   justify="left", anchor="w")
        self._full(f, self.dic_status)
        self._refresh_dictionary()


    def _build_history_tab(self, f):
        c = self.cfg
        # -- tab: history --------------------------------------------------------
        self._head(f, self.tr("settings.g_history"))
        self.v_history = tk.BooleanVar(value=bool(c.get("history", False)))
        self._bind(f, self.v_history, self.tr("history.enable"))
        self.v_history_app = tk.BooleanVar(value=bool(c.get("history_app", False)))
        self._bind(f, self.v_history_app, self.tr("history.app"))
        self._note(f, self.tr("history.note", n=history.MAX_RECORDS))
        self.his_list = tk.Listbox(f, height=7, width=50, bg=FIELD_BG,
                                   fg=FG, relief="flat", highlightthickness=0,
                                   selectbackground="#2E86DE",
                                   font=("Consolas", 9))
        self._full(f, self.his_list)
        his_btns = tk.Frame(f, bg=BG)
        for text, command in (
                (self.tr("history.refresh"), self._refresh_history),
                (self.tr("history.copy"), self._copy_selected),
                (self.tr("history.paste_again"), self._paste_again_selected),
                (self.tr("history.delete"), self._delete_selected),
                (self.tr("history.clear"), self._clear_history)):
            tk.Button(his_btns, text=text, command=command, bg="#22262E",
                      fg=FG, relief="flat", activebackground="#2A2F38",
                      activeforeground=FG, cursor="hand2").pack(
                          side="left", padx=(0, 6))
        self._full(f, his_btns)
        undo_row = tk.Frame(f, bg=BG)
        tk.Button(undo_row, text=self.tr("history.undo"), command=self._undo_last,
                  bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38", activeforeground=FG,
                  cursor="hand2").pack(side="left")
        tk.Button(undo_row, text=self.tr("history.restore"),
                  command=self._restore_clipboard, bg="#22262E", fg=FG,
                  relief="flat", activebackground="#2A2F38",
                  activeforeground=FG, cursor="hand2").pack(
                      side="left", padx=(6, 0))
        self._full(f, undo_row)
        self.his_status = tk.Label(f, text="", font=("Segoe UI", 9),
                                   fg="#9FE0A8", bg=BG, wraplength=self._px(420),
                                   justify="left", anchor="w")
        self._full(f, self.his_status)
        self._history_items = []
        self._refresh_history()


    def _build_look_tab(self, f):
        c = self.cfg
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
        devices = [""] + (self.svc.input_device_names() or [])
        self.v_device = tk.StringVar(value=c.get("input_device", ""))
        self._row(f, self.tr("settings.device"),
            self._opt(f, self.v_device, devices, width=30))
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
        # Packed before the expanding notebook: on a short window (150%
        # scaling) the notebook shrinks and the buttons stay visible.
        foot = tk.Frame(self._body, bg=BG)
        foot.pack(side="bottom", fill="x", before=self.nb)
        btns = tk.Frame(foot, bg=BG)
        btns.pack(fill="x", pady=(self._px(6), self._px(12)))
        sep = tk.Frame(foot, bg="#2A2F38", height=self._px(1))
        sep.pack(fill="x", padx=self._px(16), pady=(self._px(4), 0),
                 before=btns)
        tk.Button(btns, text=self.tr("settings.save"), command=self._save, width=12,
                  bg="#2E86DE", fg="white", relief="flat",
                  activebackground="#1B6EC2",
                  activeforeground="white").pack(
                      side="right", padx=(self._px(4), self._px(16)))
        tk.Button(btns, text=self.tr("settings.cancel"), command=self.win.destroy,
                  width=10, bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38",
                  activeforeground=FG).pack(side="right")
        tk.Button(btns, text=self.tr("settings.reset"), command=self._reset,
                  width=10, bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38",
                  activeforeground=FG).pack(
                      side="left", padx=(self._px(16), 0))
        tk.Label(btns, text=self.tr("settings.version", ver=VERSION),
                 font=("Segoe UI", 9), fg=DIM,
                 bg=BG).pack(side="left", padx=(self._px(12), 0))
        self.save_status = tk.Label(btns, text="", font=("Segoe UI", 9),
                                    fg="#9FE0A8", bg=BG)
        self.save_status.pack(side="left", padx=(self._px(12), 0))


    def _fit_to_content(self, frames):
        """Size to the tallest tab once, centred and clamped to the screen."""
        self.win.update_idletasks()
        values = tuple(frames[key] for key in
                       ("record", "model", "dictionary", "history",
                        "look", "system"))
        w = max(f.winfo_reqwidth() for f in values) + self._px(40)
        h = max(f.winfo_reqheight() for f in values) + self._px(100)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = window_layout.fit_size(w, h, sw, sh, dpi_factor(self.win))
        x, y = window_layout.place_xy(w, h, sw, sh)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _bind(self, f, var, text):
        """Checkbox bound to var, placed in frame f."""
        rows = self.rows
        rows[f] = rows.get(f, 0)
        tk.Checkbutton(f, text=text, variable=var, justify="left",
                       font=("Segoe UI", 10), fg=FG, bg=BG,
                       selectcolor=FIELD_BG, activebackground=BG,
                       activeforeground=FG, highlightthickness=0,
                       anchor="w").grid(row=rows[f], column=0, columnspan=2,
                                        sticky="w", padx=self._px(16),
                                        pady=self._px(2))
        rows[f] += 1
        return var

    def select_tab(self, key):
        """Jump to a tab by key (record/model/dictionary/history/look/system)."""
        frame = self._tab_frames.get(key)
        if frame is not None:
            self.nb.select(frame)

    # -- search, reset, inline model progress --------------------------------
    def _search(self):
        """Jump to the first setting whose label contains the query."""
        query = self.v_search.get().strip().casefold()
        if not query:
            return
        for text, key, label in self._search_index:
            if query in text.casefold():
                self.select_tab(key)
                self._flash(label)
                return
        for i in range(self.nb.index("end")):
            title = str(self.nb.tab(i, "text"))
            if query in title.casefold():
                self.nb.select(i)
                return
        self.save_status.configure(text=self.tr("settings.search_nothing"))

    def _flash(self, label):
        """Highlight a found setting for a moment, then restore its colors."""
        try:
            label.configure(bg="#2E86DE", fg="white")

            def _restore():
                if self.win.winfo_exists():
                    label.configure(bg=BG, fg="#8FB8E8")
            self.win.after(1500, _restore)
        except tk.TclError:
            pass

    def _reset(self):
        """Restore option defaults; the model, dictionary and history stay."""
        d = DEFAULTS
        self.v_silence.set(d["silence_ms"])
        self.v_partial.set(d["partial_every_s"])
        self.v_maxrec.set(d["max_record_s"])
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
        self.v_device.set(d["input_device"])
        self.v_autostart.set(d["autostart"])
        self.v_upd.set(d["update_check"])
        self.v_upd_auto.set(d["auto_update"] == "auto")
        self.v_upd_h.set(d["update_interval_h"])
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
    def _refresh_history(self):
        try:
            items = list(self.svc.history_records())
        except Exception as e:  # noqa: BLE001 - a broken file shows as empty
            self.log.warning("History read failed: %s", e)
            items = []
        self._history_items = items
        self.his_list.delete(0, "end")
        for record in reversed(items):
            stamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(float(record.get("ts") or 0)))
            text = str(record.get("text", "")).replace("\n", " ")
            short = text[:70] + ("..." if len(text) > 70 else "")
            line = f"{stamp}  {int(record.get('chars') or 0):4d}  {short}"
            app = record.get("app") or ""
            if app:
                line += "  [" + app + "]"
            self.his_list.insert("end", line)
        if not items:
            key = ("history.empty" if self.v_history.get()
                   else "history.disabled")
            self.his_status.configure(text=self.tr(key))

    def _selected_history(self):
        selected = self.his_list.curselection()
        if not selected:
            return None
        shown = int(selected[0])
        index = len(self._history_items) - 1 - shown
        if 0 <= index < len(self._history_items):
            return index, self._history_items[index]
        return None

    def _copy_selected(self):
        picked = self._selected_history()
        if picked:
            self.svc.history_copy(picked[1].get("text", ""))

    def _paste_again_selected(self):
        picked = self._selected_history()
        if not picked:
            return
        self.svc.history_copy(picked[1].get("text", ""),
                              hint="history.paste_again_hint")
        self.win.destroy()

    def _delete_selected(self):
        picked = self._selected_history()
        if not picked:
            return
        self.svc.history_delete(picked[0])
        self._refresh_history()
        self.his_status.configure(text=self.tr("history.deleted"))

    def _clear_history(self):
        if not messagebox.askyesno(self.tr("history.clear"),
                                   self.tr("history.clear_question"),
                                   parent=self.win):
            return
        self.svc.history_clear()
        self._refresh_history()
        self.his_status.configure(text=self.tr("history.cleared"))

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
        c["input_device"] = self.v_device.get().strip() or c.get("input_device", "")
        c["autostart"] = bool(self.v_autostart.get())
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
