"""Settings dialog (tkinter Toplevel) — lives on the HUD main thread.

Localized through i18n (en/ru shipped). Organized into tabs (ttk.Notebook):
Recording / Model & language / Appearance / System — the window stays
compact no matter how many settings land. The model tab switches models
through a progress dialog (download -> load -> remove old on request).
"""
import ctypes
import tkinter as tk
from tkinter import ttk

import i18n
import onboarding
from config import LANG_NAMES, LANGS, MODELS, MODEL_GB, VERSION

BG = "#16181D"
FG = "#D5D9E0"
DIM = "#9AA3B0"
FIELD_BG = "#22262E"


def dpi_factor(win):
    """Windows scaling factor (1.0 at 100%, 1.5 at 150%) for fixed-pixel sizes."""
    try:
        return max(1.0, win.winfo_fpixels("1i") / 96.0)
    except Exception:
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
        self.win.title(tr("dlg.model_title"))
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        tk.Label(self.win, text=tr("dlg.model_title"),
                 font=("Segoe UI", 11, "bold"), fg="#8FB8E8",
                 bg=BG).pack(fill="x", padx=px(16), pady=(px(12), px(4)))
        self.lbl = tk.Label(self.win, text="…", font=("Segoe UI", 10),
                            fg=FG, bg=BG, wraplength=px(330), justify="left")
        self.lbl.pack(fill="x", padx=px(16))
        self.bar = ttk.Progressbar(self.win, mode="indeterminate",
                                   length=px(330))
        self.bar.pack(fill="x", padx=px(16), pady=(px(8), px(4)))
        self.bar.start(12)
        self.btn = tk.Button(self.win, text=tr("dlg.close"),
                             state="disabled", command=self.win.destroy,
                             width=12, bg="#22262E", fg=FG, relief="flat",
                             activebackground="#2A2F38", activeforeground=FG)
        self.btn.pack(pady=(px(4), px(12)))
        try:
            self.win.geometry("+%d+%d" % (master.winfo_rootx() + px(40),
                                          master.winfo_rooty() + px(40)))
        except Exception:
            pass
        self.win.after(200, self._poll)

    def _text(self, p):
        ph = p.get("phase")
        if ph == "download":
            name = p.get("name", "")
            return self.tr("dlg.phase_download", name=name,
                           gb=MODEL_GB.get(name, "?"))
        if ph == "load":
            return self.tr("dlg.phase_load", name=p.get("name", ""))
        if ph == "remove":
            return self.tr("dlg.phase_remove", name=p.get("name", ""))
        if ph == "done":
            t = self.tr("dlg.phase_done", name=p.get("name", ""))
            if p.get("cache_note"):
                t += "\n" + self.tr("dlg.cache_note", name=p["cache_note"])
            return t
        if ph == "busy":
            return self.tr("dlg.phase_busy")
        if ph == "error":
            return self.tr("dlg.phase_error", detail=p.get("detail", ""))
        return "…"

    def _poll(self):
        try:
            if not self.win.winfo_exists():
                return
            p = dict(self.progress)
            self.lbl.configure(text=self._text(p))
            if p.get("phase") in ("done", "error", "busy"):
                self.bar.stop()
                self.btn.configure(state="normal")
                return   # stop polling; the window stays until user closes it
        except Exception:
            pass
        self.win.after(200, self._poll)


class SettingsWindow:
    def __init__(self, master, svc, log):
        self.svc = svc
        self.cfg = svc.cfg
        self.log = log
        self.master = master
        tr = lambda key, **kw: svc.tr(key, **kw)  # noqa: E731
        self.tr = tr
        c = self.cfg
        px = lambda n: int(round(n * dpi_factor(self.win)))  # noqa: E731

        self.win = tk.Toplevel(master)
        self.win.title(tr("settings.title"))
        self.win.configure(bg=BG)
        self.win.resizable(True, True)

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True)

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Vibe.TNotebook", background=BG, borderwidth=0,
                        tabmargins=(8, 4, 8, 0))
        # clam ignores a static foreground on tabs — drive ALL text colors
        # through the state map (static fg renders as near-invisible dark)
        style.configure("Vibe.TNotebook.Tab", background=FIELD_BG,
                        padding=(10, 4), font=("Segoe UI", 10))
        style.map("Vibe.TNotebook.Tab",
                  background=[("selected", "#2E86DE")],
                  foreground=[("selected", "white"), ("!selected", FG)])

        nb = self.nb = ttk.Notebook(body, style="Vibe.TNotebook")
        nb.pack(fill="both", expand=True)
        f_rec = tk.Frame(nb, bg=BG)
        f_mod = tk.Frame(nb, bg=BG)
        f_look = tk.Frame(nb, bg=BG)
        f_sys = tk.Frame(nb, bg=BG)
        nb.add(f_rec, text=tr("tabs.record"))
        nb.add(f_mod, text=tr("tabs.model"))
        nb.add(f_look, text=tr("tabs.look"))
        nb.add(f_sys, text=tr("tabs.system"))

        rows = self.rows = {}   # shared with _bind() below

        def head(f, text):
            rows[f] = rows.get(f, 0)
            tk.Label(f, text=text, font=("Segoe UI", 10, "bold"),
                     fg="#8FB8E8", bg=BG).grid(row=rows[f], column=0,
                                               columnspan=2, sticky="w",
                                               padx=16, pady=(12, 2))
            rows[f] += 1

        def row(f, label, widget):
            rows[f] = rows.get(f, 0)
            tk.Label(f, text=label, font=("Segoe UI", 10), fg=FG,
                     bg=BG).grid(row=rows[f], column=0, sticky="w",
                                 padx=16, pady=2)
            widget.grid(row=rows[f], column=1, sticky="e", padx=16, pady=2)
            rows[f] += 1

        def full(f, widget):
            rows[f] = rows.get(f, 0)
            widget.grid(row=rows[f], column=0, columnspan=2, sticky="w",
                        padx=16, pady=2)
            rows[f] += 1

        def note(f, text):
            rows[f] = rows.get(f, 0)
            tk.Label(f, text=text, font=("Segoe UI", 9), fg=DIM,
                     bg=BG, wraplength=px(400), justify="left").grid(
                row=rows[f], column=0, columnspan=2, sticky="w", padx=16)
            rows[f] += 1

        def scale(f, from_, to, resolution, var, command=None):
            return tk.Scale(f, from_=from_, to=to, resolution=resolution,
                            orient="horizontal", length=px(210), width=px(14),
                            showvalue=True, variable=var, bg=FIELD_BG, fg=FG,
                            highlightthickness=0, troughcolor="#2A2F38",
                            font=("Segoe UI", 9),
                            command=command)

        def opt(f, var, values, width=None):
            m = tk.OptionMenu(f, var, *values)
            m.configure(bg=FIELD_BG, fg=FG, activebackground="#2A2F38",
                        activeforeground=FG, relief="flat",
                        highlightthickness=0, anchor="w" if not width else "e")
            m["menu"].configure(bg=FIELD_BG, fg=FG, activebackground="#2E86DE",
                                activeforeground="white", font=("Segoe UI", 10))
            if width:
                m.configure(width=width)
            return m

        # -- tab: recording --------------------------------------------------
        head(f_rec, tr("settings.g_pauses"))
        self.v_silence = tk.IntVar(value=int(c.get("silence_ms", 2000)))
        row(f_rec, tr("settings.silence"), scale(f_rec, 500, 8000, 100,
                                                 self.v_silence))
        note(f_rec, tr("settings.note_silence"))
        self.v_autostop = tk.BooleanVar(value=bool(c.get("auto_stop", True)))
        self._bind(f_rec, self.v_autostop, tr("settings.autostop"))
        self.v_partial = tk.DoubleVar(value=float(c.get("partial_every_s", 1.5)))
        row(f_rec, tr("settings.partial"), scale(f_rec, 0.5, 5.0, 0.5,
                                                 self.v_partial))
        note(f_rec, tr("settings.note_partial"))
        self.v_maxrec = tk.IntVar(value=int(c.get("max_record_s", 120)))
        row(f_rec, tr("settings.maxrec"), scale(f_rec, 30, 600, 30, self.v_maxrec))
        head(f_rec, tr("settings.g_sound"))
        self.v_beep_on = tk.BooleanVar(value=bool(c.get("beep_on", True)))
        self._bind(f_rec, self.v_beep_on, tr("settings.beep_on"))
        self.v_beep_off = tk.BooleanVar(value=bool(c.get("beep_off", True)))
        self._bind(f_rec, self.v_beep_off, tr("settings.beep_off"))

        # -- tab: model & language --------------------------------------------
        head(f_mod, tr("model.g"))
        self.v_model = tk.StringVar(value=str(c.get("model", "large-v3-turbo")))
        row(f_mod, tr("model.choose"), opt(f_mod, self.v_model, MODELS, width=16))
        self.model_desc = tk.Label(f_mod, text="", font=("Segoe UI", 9),
                                   fg=DIM, bg=BG, wraplength=px(400),
                                   justify="left")
        self.model_desc.grid(row=rows.get(f_mod, 0), column=0, columnspan=2,
                             sticky="w", padx=16)
        rows[f_mod] = rows.get(f_mod, 0) + 1
        self._update_model_desc()
        self.v_model.trace_add("write", lambda *a: self._update_model_desc())
        self.v_keep = tk.BooleanVar(value=bool(c.get("model_keep_old", True)))
        self._bind(f_mod, self.v_keep, tr("model.keep"))
        head(f_mod, tr("settings.g_lang"))
        lang_pairs = [(LANG_NAMES.get(x, x), x) for x in LANGS]
        self._lang_by_name = dict(lang_pairs)
        cur_l = str(c.get("language", "en"))
        cur_l_disp = next((d for d, cd in lang_pairs if cd == cur_l), cur_l)
        self.v_lang = tk.StringVar(value=cur_l_disp)
        row(f_mod, tr("settings.recog_lang"),
            opt(f_mod, self.v_lang, [d for d, _ in lang_pairs], width=16))
        note(f_mod, tr("settings.note_recog"))
        ui_pairs = [("auto", "auto")] + [
            (LANG_NAMES.get(x, x), x) for x in i18n.available_locales()]
        self._ui_by_name = dict(ui_pairs)
        cur_ui = str(c.get("locale", "auto"))
        cur_ui_disp = next((d for d, cd in ui_pairs if cd == cur_ui), cur_ui)
        self.v_ui = tk.StringVar(value=cur_ui_disp)
        row(f_mod, tr("settings.ui_lang"),
            opt(f_mod, self.v_ui, [d for d, _ in ui_pairs], width=16))
        note(f_mod, tr("settings.note_ui"))

        # -- tab: appearance ---------------------------------------------------
        head(f_look, tr("settings.g_panel"))
        self.v_alpha = tk.DoubleVar(value=float(c.get("hud_alpha", 0.94)))
        row(f_look, tr("settings.alpha"),
            scale(f_look, 0.4, 1.0, 0.05, self.v_alpha,
                  command=lambda v: self.svc.live.__setitem__("alpha", float(v))))
        note(f_look, tr("settings.note_alpha"))
        self.v_autogrow = tk.BooleanVar(value=bool(c.get("hud_autogrow", True)))
        self._bind(f_look, self.v_autogrow, tr("settings.autogrow"))
        self.v_hud = tk.BooleanVar(value=bool(c.get("hud", True)))
        self._bind(f_look, self.v_hud, tr("settings.show_panel"))

        # -- tab: system -------------------------------------------------------
        head(f_sys, tr("settings.g_paste"))
        self.v_paste = tk.StringVar(value=c.get("paste_method", "shift_insert"))
        row(f_sys, tr("settings.paste"),
            opt(f_sys, self.v_paste, ("shift_insert", "ctrl_v"), width=16))
        self.v_restore = tk.BooleanVar(value=bool(c.get("restore_clipboard", True)))
        self._bind(f_sys, self.v_restore, tr("settings.restore"))
        self.v_confirm = tk.BooleanVar(
            value=bool(c.get("confirm_focus_change", True)))
        self._bind(f_sys, self.v_confirm, tr("settings.confirm_focus"))
        self.v_exclude = tk.BooleanVar(
            value=bool(c.get("clipboard_exclude_history", True)))
        self._bind(f_sys, self.v_exclude, tr("settings.exclude_history"))
        note(f_sys, tr("settings.note_exclude_history"))
        self.v_notify_text = tk.BooleanVar(
            value=bool(c.get("notify_text", False)))
        self._bind(f_sys, self.v_notify_text, tr("settings.notify_text"))
        head(f_sys, tr("sys.g_startup"))
        self.v_hotkey = tk.StringVar(value=c.get("hotkey", "ctrl+alt+space"))
        row(f_sys, tr("settings.hotkey"),
            tk.Entry(f_sys, textvariable=self.v_hotkey, width=18,
                     bg=FIELD_BG, fg=FG, insertbackground=FG, relief="flat"))
        note(f_sys, tr("settings.note_hotkey"))
        devices = [""] + (self.svc.input_device_names() or [])
        self.v_device = tk.StringVar(value=c.get("input_device", ""))
        row(f_sys, tr("settings.device"),
            opt(f_sys, self.v_device, devices, width=30))
        self.v_autostart = tk.BooleanVar(value=bool(c.get("autostart", True)))
        self._bind(f_sys, self.v_autostart, tr("settings.autostart_w"))
        btn_setup = tk.Button(f_sys, text=tr("settings.onboarding"),
                              command=self.svc.open_onboarding, width=22,
                              bg="#22262E", fg=FG, relief="flat",
                              activebackground="#2A2F38", cursor="hand2")
        full(f_sys, btn_setup)
        head(f_sys, tr("updates.g"))
        self.v_upd = tk.BooleanVar(value=bool(c.get("update_check", True)))
        self._bind(f_sys, self.v_upd, tr("updates.auto"))
        self.v_upd_auto = tk.BooleanVar(
            value=c.get("auto_update") == "auto")
        self._bind(f_sys, self.v_upd_auto, tr("updates.install_auto"))
        self.v_upd_h = tk.IntVar(value=int(c.get("update_interval_h", 2)))
        row(f_sys, tr("updates.interval"), scale(f_sys, 1, 24, 1, self.v_upd_h))
        btn_upd = tk.Button(f_sys, text=tr("updates.check_now"),
                            command=self.svc.check_updates_now, width=16,
                            bg="#22262E", fg=FG, relief="flat",
                            activebackground="#2A2F38", activeforeground=FG,
                            cursor="hand2")
        full(f_sys, btn_upd)
        self.ram_lbl = tk.Label(f_sys, text="", font=("Segoe UI", 9),
                                fg="#9FE0A8", bg=BG, anchor="w")
        self.ram_lbl.grid(row=rows[f_sys], column=0, columnspan=2,
                          sticky="w", padx=16)
        rows[f_sys] += 1
        self._refresh_ram()

        # -- buttons under the notebook ------------------------------------------
        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x", pady=(6, 12))
        tk.Button(btns, text=tr("settings.save"), command=self._save, width=12,
                  bg="#2E86DE", fg="white", relief="flat",
                  activebackground="#1B6EC2",
                  activeforeground="white").pack(side="right", padx=(4, 16))
        tk.Button(btns, text=tr("settings.cancel"), command=self.win.destroy,
                  width=10, bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38",
                  activeforeground=FG).pack(side="right")
        tk.Label(btns, text=tr("settings.version", ver=VERSION),
                 font=("Segoe UI", 9), fg=DIM,
                 bg=BG).pack(side="left", padx=(16, 0))

        body.columnconfigure(0, weight=1)

        # -- size to the tallest tab, nothing clipped ----------------------------
        self.win.update_idletasks()
        frames = (f_rec, f_mod, f_look, f_sys)
        w = max(f.winfo_reqwidth() for f in frames) + 16
        h = max(f.winfo_reqheight() for f in frames) + 40 + 50 + 10
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry("%dx%d+140+60" % (min(w, sw - 40), min(h, sh - 80)))

    def _bind(self, f, var, text):
        """Checkbox bound to var, placed in frame f."""
        rows = self.rows
        rows[f] = rows.get(f, 0)
        tk.Checkbutton(f, text=text, variable=var, justify="left",
                       font=("Segoe UI", 10), fg=FG, bg=BG,
                       selectcolor=FIELD_BG, activebackground=BG,
                       activeforeground=FG, highlightthickness=0,
                       anchor="w").grid(row=rows[f], column=0, columnspan=2,
                                        sticky="w", padx=16, pady=2)
        rows[f] += 1
        return var

    def _update_model_desc(self):
        try:
            self.model_desc.configure(
                text=self.tr("models.desc.%s" % self.v_model.get()))
        except Exception:
            pass

    def _refresh_ram(self):
        try:
            if not self.win.winfo_exists():
                return
            mb = process_ram_mb()
            self.ram_lbl.configure(text=self.tr("settings.ram", mb=mb))
        except Exception as e:
            self.ram_lbl.configure(text="RAM: %s" % e)
        self.win.after(1500, self._refresh_ram)

    def _save(self):
        c = dict(self.cfg)
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
        self.win.destroy()
        if model_changed:
            progress = {"phase": "download", "name": new_model}
            ModelSwitchDialog(self.master, progress, self.tr)
            self.svc.switch_model(new_model, bool(self.v_keep.get()), progress)
