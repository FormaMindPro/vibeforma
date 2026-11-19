"""Settings dialog (tkinter Toplevel) — lives on the HUD main thread.

Sliders for pause/transparency, live RAM readout, autogrow toggle.
The window sizes itself to its content (geometry from winfo_reqsize),
so nothing is clipped. Save applies to the live service w/o restart.
"""
import ctypes
import tkinter as tk

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
        c = self.cfg

        self.win = tk.Toplevel(master)
        self.win.title("Настройки диктовки")
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

        # -- паузы и запись ------------------------------------------------
        head("Паузы и запись")
        self.v_silence = tk.IntVar(value=int(c.get("silence_ms", 2000)))
        row("Пауза тишины, мс", scale(500, 8000, 100, self.v_silence))
        note("пауза в речи, после которой фраза закрывается и вставляется")
        self.v_autostop = tk.BooleanVar(value=bool(c.get("auto_stop", True)))
        full(check(self.v_autostop,
                   "автостоп по тишине (выкл — остановка только повторным хоткеем)"))
        self.v_partial = tk.DoubleVar(value=float(c.get("partial_every_s", 1.5)))
        row("Стриминг текста, с", scale(0.5, 5.0, 0.5, self.v_partial))
        note("как часто панель обновляет распознанный текст во время речи")
        self.v_maxrec = tk.IntVar(value=int(c.get("max_record_s", 120)))
        row("Макс. запись, с", scale(30, 600, 30, self.v_maxrec))

        # -- внешний вид ----------------------------------------------------
        head("Панель мониторинга")
        self.v_alpha = tk.DoubleVar(value=float(c.get("hud_alpha", 0.94)))
        row("Прозрачность",
            scale(0.4, 1.0, 0.05, self.v_alpha,
                  command=lambda v: self.svc.live.__setitem__("alpha", float(v))))
        note("слайдер меняет прозрачность панели сразу, «Сохранить» запоминает")
        self.v_autogrow = tk.BooleanVar(value=bool(c.get("hud_autogrow", True)))
        full(check(self.v_autogrow,
                   "окно растёт с текстом (выкл — фиксированная высота)"))
        self.v_hud = tk.BooleanVar(value=bool(c.get("hud", True)))
        full(check(self.v_hud, "показывать панель мониторинга"))

        # -- вставка ----------------------------------------------------------
        head("Вставка")
        self.v_paste = tk.StringVar(value=c.get("paste_method", "shift_insert"))
        row("Способ вставки", tk.OptionMenu(body, self.v_paste,
                                            "shift_insert", "ctrl_v"))
        self.v_restore = tk.BooleanVar(value=bool(c.get("restore_clipboard", True)))
        full(check(self.v_restore,
                   "возвращать прежний буфер обмена через 1 с после вставки"))

        # -- звук / запуск ----------------------------------------------------
        head("Звук и запуск")
        self.v_beep_on = tk.BooleanVar(value=bool(c.get("beep_on", True)))
        full(check(self.v_beep_on, "звук начала записи"))
        self.v_beep_off = tk.BooleanVar(value=bool(c.get("beep_off", True)))
        full(check(self.v_beep_off, "звук конца записи"))
        self.v_hotkey = tk.StringVar(value=c.get("hotkey", "ctrl+alt+space"))
        row("Хоткей", tk.Entry(body, textvariable=self.v_hotkey, width=18,
                               bg=FIELD_BG, fg=FG, insertbackground=FG,
                               relief="flat"))
        note("формат: ctrl+alt+space — применяется по «Сохранить»")
        devices = [""] + (self.svc.input_device_names() or [])
        self.v_device = tk.StringVar(value=c.get("input_device", ""))
        dev_menu = tk.OptionMenu(body, self.v_device, *devices)
        dev_menu.configure(width=30, anchor="e")
        row("Устройство ввода", dev_menu)
        self.v_autostart = tk.BooleanVar(value=bool(c.get("autostart", True)))
        full(check(self.v_autostart, "запускать сервис при входе в Windows"))

        # -- память ------------------------------------------------------------
        self.ram_lbl = tk.Label(body, text="", font=("Segoe UI", 9),
                                fg="#9FE0A8", bg=BG, anchor="w")
        self.ram_lbl.grid(row=r, column=0, columnspan=2, sticky="w", padx=16)
        r += 1
        self._refresh_ram()

        btns = tk.Frame(body, bg=BG)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", padx=16,
                  pady=(10, 12))
        tk.Button(btns, text="Сохранить", command=self._save, width=12,
                  bg="#2E86DE", fg="white", relief="flat",
                  activebackground="#1B6EC2",
                  activeforeground="white").pack(side="left", padx=4)
        tk.Button(btns, text="Отмена", command=self.win.destroy, width=10,
                  bg="#22262E", fg=FG, relief="flat",
                  activebackground="#2A2F38",
                  activeforeground=FG).pack(side="left")

        body.columnconfigure(0, weight=1)

        # -- размер по содержимому, ничего не обрезается ----------------------
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
            self.ram_lbl.configure(text="Память сервиса с моделью: %d МБ" % mb)
        except Exception as e:
            self.ram_lbl.configure(text="память: %s" % e)
        self.win.after(1500, self._refresh_ram)

    def _save(self):
        c = dict(self.cfg)
        c["silence_ms"] = int(self.v_silence.get())
        c["partial_every_s"] = float(self.v_partial.get())
        c["max_record_s"] = int(self.v_maxrec.get())
        c["auto_stop"] = bool(self.v_autostop.get())
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
            self.log.exception("Сохранение настроек не удалось: %s", e)
        self.win.destroy()
