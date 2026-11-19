"""Settings dialog (tkinter Toplevel) — lives on the HUD main thread.

Localized through i18n (en/ru shipped). Adds recognition-language and
interface-language pickers. The window sizes itself to its content
(geometry from winfo_reqsize), so nothing is clipped. Save applies to the
live service w/o restart.
"""
import ctypes
import tkinter as tk

import i18n
from config import LANGS

BG = "#16181D"
FG = "#D5D9E0"
DIM = "#7A8290"
FIELD_BG = "#22262E"

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


class SettingsWindow:
    def __init__(self, master, svc, log):
        self.svc = svc
        self.cfg = svc.cfg
        self.log = log
        tr = lambda key, **kw: svc.tr(key, **kw)  # noqa: E731
        c = self.cfg

        self.win = tk.Toplevel(master)
        self.win.title(tr("settings.title"))
        self.win.configure(bg=BG)
        self.win.resizable(True, True)

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True)

        r = 0

        def head(text):
            nonlocal r
            tk.Label(body, text=text, font=("Segoe UI", 10, "bold"),
                     fg="#8FB8E8", bg=BG).grid(row=r, column=0, columnspan=2,
                                               sticky="w", padx=16, pady=(12, 2))
            r += 1

        def row(label, widget):
            nonlocal r
            tk.Label(body, text=label, font=("Segoe UI", 10), fg=FG,
                     bg=BG).grid(row=r, column=0, sticky="w", padx=16, pady=2)
            widget.grid(row=r, column=1, sticky="e", padx=16, pady=2)
            r += 1

        def full(widget):
            nonlocal r
            widget.grid(row=r, column=0, columnspan=2, sticky="w",
                        padx=16, pady=2)
            r += 1

        def note(text):
            nonlocal r
            tk.Label(body, text=text, font=("Segoe UI", 8), fg=DIM,
                     bg=BG).grid(row=r, column=0, columnspan=2, sticky="w",
                                 padx=16)
            r += 1

        def check(var, text):
            return tk.Checkbutton(body, text=text, variable=var, justify="left",
                                  font=("Segoe UI", 10), fg=FG, bg=BG,
                                  selectcolor=FIELD_BG, activebackground=BG,
                                  activeforeground=FG, highlightthickness=0,
                                  anchor="w")

        def scale(from_, to, resolution, var, command=None):
            return tk.Scale(body, from_=from_, to=to, resolution=resolution,
                            orient="horizontal", length=200, width=14,
                            showvalue=True, variable=var, bg=FIELD_BG, fg=FG,
                            highlightthickness=0, troughcolor="#2A2F38",
                            command=command)

        # -- pauses & recording ----------------------------------------------
        head(tr("settings.g_pauses"))
        self.v_silence = tk.IntVar(value=int(c.get("silence_ms", 2000)))
        row(tr("settings.silence"), scale(500, 8000, 100, self.v_silence))
        note(tr("settings.note_silence"))
        self.v_autostop = tk.BooleanVar(value=bool(c.get("auto_stop", True)))
        full(check(self.v_autostop, tr("settings.autostop")))
        self.v_partial = tk.DoubleVar(value=float(c.get("partial_every_s", 1.5)))
        row(tr("settings.partial"), scale(0.5, 5.0, 0.5, self.v_partial))
        note(tr("settings.note_partial"))
        self.v_maxrec = tk.IntVar(value=int(c.get("max_record_s", 120)))
        row(tr("settings.maxrec"), scale(30, 600, 30, self.v_maxrec))

        # -- language ----------------------------------------------------------
        head(tr("settings.g_lang"))
        self.v_lang = tk.StringVar(value=str(c.get("language", "auto")))
        row(tr("settings.recog_lang"), tk.OptionMenu(body, self.v_lang, *LANGS))
        note(tr("settings.note_recog"))
        self.v_ui = tk.StringVar(value=str(c.get("locale", "auto")))
        row(tr("settings.ui_lang"),
            tk.OptionMenu(body, self.v_ui, "auto", *i18n.available_locales()))
        note(tr("settings.note_ui"))

        # -- monitoring panel ---------------------------------------------------
        head(tr("settings.g_panel"))
        self.v_alpha = tk.DoubleVar(value=float(c.get("hud_alpha", 0.94)))
        row(tr("settings.alpha"),
            scale(0.4, 1.0, 0.05, self.v_alpha,
                  command=lambda v: self.svc.live.__setitem__("alpha", float(v))))
        note(tr("settings.note_alpha"))
        self.v_autogrow = tk.BooleanVar(value=bool(c.get("hud_autogrow", True)))
        full(check(self.v_autogrow, tr("settings.autogrow")))
        self.v_hud = tk.BooleanVar(value=bool(c.get("hud", True)))
        full(check(self.v_hud, tr("settings.show_panel")))

        # -- pasting -------------------------------------------------------------
        head(tr("settings.g_paste"))
        self.v_paste = tk.StringVar(value=c.get("paste_method", "shift_insert"))
        row(tr("settings.paste"), tk.OptionMenu(body, self.v_paste,
                                                "shift_insert", "ctrl_v"))
        self.v_restore = tk.BooleanVar(value=bool(c.get("restore_clipboard", True)))
        full(check(self.v_restore, tr("settings.restore")))

        # -- sound / startup -------------------------------------------------------
        head(tr("settings.g_sound"))
        self.v_beep_on = tk.BooleanVar(value=bool(c.get("beep_on", True)))
        full(check(self.v_beep_on, tr("settings.beep_on")))
        self.v_beep_off = tk.BooleanVar(value=bool(c.get("beep_off", True)))
        full(check(self.v_beep_off, tr("settings.beep_off")))
        self.v_hotkey = tk.StringVar(value=c.get("hotkey", "ctrl+alt+space"))
        row(tr("settings.hotkey"), tk.Entry(body, textvariable=self.v_hotkey,
                                            width=18, bg=FIELD_BG, fg=FG,
                                            insertbackground=FG, relief="flat"))
        note(tr("settings.note_hotkey"))
        devices = [""] + (self.svc.input_device_names() or [])
        self.v_device = tk.StringVar(value=c.get("input_device", ""))
        dev_menu = tk.OptionMenu(body, self.v_device, *devices)
        dev_menu.configure(width=30, anchor="e")
        row(tr("settings.device"), dev_menu)
        self.v_autostart = tk.BooleanVar(value=bool(c.get("autostart", True)))
        full(check(self.v_autostart, tr("settings.autostart_w")))

        # -- memory ------------------------------------------------------------
        self.ram_lbl = tk.Label(body, text="", font=("Segoe UI", 9),
                                fg="#9FE0A8", bg=BG, anchor="w")
        self.ram_lbl.grid(row=r, column=0, columnspan=2, sticky="w", padx=16)
        r += 1
        self._refresh_ram()

        btns = tk.Frame(body, bg=BG)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", padx=16,
                  pady=(10, 12))
        tk.Button(btns, text=tr("settings.save"), command=self._save, width=12,
                  bg="#2E86DE", fg="white", relief="flat",
                  activebackground="#1B6EC2",
                  activeforeground="white").pack(side="left", padx=4)
        tk.Button(btns, text=tr("settings.cancel"), command=self.win.destroy,
                  width=10, bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38",
                  activeforeground=FG).pack(side="left")

        body.columnconfigure(0, weight=1)

        # -- size to content, nothing clipped -----------------------------------
        self.win.update_idletasks()
        w_req = self.win.winfo_reqwidth()
        h_req = self.win.winfo_reqheight()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w_use = min(w_req + 10, sw - 40)
        h_use = min(h_req + 10, sh - 80)
        self.win.geometry("%dx%d+140+60" % (w_use, h_use))

    def _refresh_ram(self):
        try:
            if not self.win.winfo_exists():
                return
            mb = process_ram_mb()
            self.ram_lbl.configure(
                text=self.svc.tr("settings.ram", mb=mb))
        except Exception as e:
            self.ram_lbl.configure(text="RAM: %s" % e)
        self.win.after(1500, self._refresh_ram)

    def _save(self):
        c = dict(self.cfg)
        c["silence_ms"] = int(self.v_silence.get())
        c["partial_every_s"] = float(self.v_partial.get())
        c["max_record_s"] = int(self.v_maxrec.get())
        c["auto_stop"] = bool(self.v_autostop.get())
        c["language"] = self.v_lang.get()
        c["locale"] = self.v_ui.get()
        c["paste_method"] = self.v_paste.get()
        c["restore_clipboard"] = bool(self.v_restore.get())
        c["hud"] = bool(self.v_hud.get())
        c["hud_alpha"] = float(self.v_alpha.get())
        c["hud_autogrow"] = bool(self.v_autogrow.get())
        c["beep_on"] = bool(self.v_beep_on.get())
        c["beep_off"] = bool(self.v_beep_off.get())
        c["hotkey"] = self.v_hotkey.get().strip() or c.get("hotkey", "ctrl+alt+space")
        c["input_device"] = self.v_device.get().strip() or c.get("input_device", "")
        c["autostart"] = bool(self.v_autostart.get())
        try:
            self.svc.apply_settings(c)
        except Exception as e:
            self.log.exception("Saving settings failed: %s", e)
        self.win.destroy()
