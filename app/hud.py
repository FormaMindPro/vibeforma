"""Live monitoring panel (HUD): mic level, VAD, streaming partial text.

Tk runs in its own daemon thread; only that thread touches the widgets.
The state dict is written by service threads — single reads are GIL-safe.
The eye button switches the privacy mode: the text is hidden, the level
stays. The status line disappears LAST_TTL_S seconds after it was set.
"""
import time
import tkinter as tk

STATE_COLORS = {
    "loading": "#F39C12",
    "idle": "#2E86DE",
    "recording": "#E74C3C",
    "processing": "#F39C12",
    "paused": "#5D6D7E",
    "error": "#8E44AD",
    "updating": "#8FB8E8",
}
BG = "#16181D"
FG = "#D5D9E0"
DIM = "#7A8290"

W = 380
BASE_TEXT_LINES = 4
LINE_H = 20
BASE_H = 190
LAST_TTL_S = 30


def dpi_factor(win):
    """Windows scaling factor (1.0 at 100%, 1.5 at 150%) for fixed-pixel sizes."""
    try:
        return max(1.0, win.winfo_fpixels("1i") / 96.0)
    except Exception:
        return 1.0


class Hud:
    def __init__(self, svc, log):
        self.svc = svc
        self.state = svc.live       # shared dict, written by service threads
        self.cfg = svc.cfg
        self.log = log
        self._drag = None
        self._settings = None
        self._onboarding = None
        self._root_h = BASE_H

        self.root = tk.Tk()
        px = self._px = lambda n: int(round(n * dpi_factor(self.root)))  # noqa: E731
        self._W = px(W)
        self._line_h = px(LINE_H)
        base_h = px(BASE_H)
        self._root_h = base_h

        self.root.title("VibeForma")
        self.root.geometry("%dx%d+%d+%d" % self._place(base_h))
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)   # borderless
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", float(self.cfg.get("hud_alpha", 0.94)))

        # -- header: [⚙]  status …  |  [🎤] [✕] ----------------------------
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=px(12), pady=(px(10), px(2)))
        gear = tk.Label(top, text="⚙", font=("Segoe UI", 12), fg=FG, bg=BG,
                        cursor="hand2")
        gear.pack(side="left")
        gear.bind("<Button-1>", lambda e: self._open_settings())
        self.eye = tk.Label(top, text="👁", font=("Segoe UI", 11), fg=FG,
                            bg=BG, cursor="hand2")
        self.eye.pack(side="left", padx=(px(8), 0))
        self.eye.bind("<Button-1>", lambda e: self._toggle_privacy())
        self.dot = tk.Canvas(top, width=px(14), height=px(14), bg=BG,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(px(10), px(4)))
        self.lbl = tk.Label(top, text="…", font=("Segoe UI", 11, "bold"),
                            fg=FG, bg=BG, anchor="w")
        self.lbl.pack(side="left")
        # record button: click = the same state-guarded toggle as the hotkey
        self.mic = tk.Label(top, text="🎤", font=("Segoe UI", 13), fg=FG,
                            bg=BG, cursor="hand2", padx=px(6))
        self.mic.bind("<Button-1>", lambda e: self._on_mic())
        xlab = tk.Label(top, text="✕", font=("Segoe UI", 11), fg=DIM, bg=BG,
                        cursor="hand2")
        xlab.bind("<Button-1>", lambda e: self.root.withdraw())
        sep = tk.Frame(top, width=px(1), height=px(16), bg="#3A4048")
        xlab.pack(side="right")
        sep.pack(side="right", padx=(px(4), px(8)))
        self.mic.pack(side="right")
        self._bind_drag(self.dot)
        self._bind_drag(self.lbl)
        self._bind_drag(top)

        # -- level bar + device hint ---------------------------------------
        self.bar = tk.Canvas(self.root, height=px(10), bg="#22262E",
                             highlightthickness=0)
        self.bar.pack(fill="x", padx=px(12), pady=(px(6), 0))
        self._bind_drag(self.bar)
        self.hint = tk.Label(self.root, text="", font=("Segoe UI", 8),
                             fg=DIM, bg=BG, anchor="w")
        self.hint.pack(fill="x", padx=px(12))
        self._bind_drag(self.hint)

        # -- live text -------------------------------------------------------
        self.text = tk.Text(self.root, height=BASE_TEXT_LINES, wrap="word",
                            font=("Segoe UI", 10), fg=FG, bg="#1D2027",
                            relief="flat", padx=px(8), pady=px(6))
        self.text.pack(fill="both", expand=True, padx=px(12), pady=(px(6), px(4)))
        self.text.insert("1.0", "…")
        self.text.configure(state="disabled")
        try:
            import tkinter.font as tkfont
            f = tkfont.Font(font=self.text.cget("font"))
            self._line_h = max(px(LINE_H), int(f.metrics("linespace")))
        except Exception:
            pass

        self.last = tk.Label(self.root, text="", font=("Segoe UI", 8),
                             fg=DIM, bg=BG, anchor="w")
        self.last.pack(fill="x", padx=px(12), pady=(0, px(8)))
        self._bind_drag(self.last)

        self.root.bind("<Escape>", lambda e: self.root.withdraw())
        self.root.after(120, self._tick)

    # -- layout / drag -----------------------------------------------------
    def _place(self, base_h):
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            return self._W, base_h, sw - self._W - self._px(28), \
                sh - base_h - self._px(72)
        except Exception:
            return self._W, base_h, 100, 100

    def _bind_drag(self, widget):
        widget.bind("<Button-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry("+%d+%d" % (e.x_root - self._drag[0],
                                           e.y_root - self._drag[1]))

    def _open_settings(self):
        self.state["open_settings"] = True
        try:
            if not self.root.winfo_viewable():
                self.root.deiconify()
        except Exception:
            pass

    def _state_label(self, key):
        label = self.svc.tr("states.%s" % key, hotkey=self.cfg.get("hotkey", ""))
        return key if label == "states.%s" % key else label

    def _on_mic(self):
        """Record button: the same state-guarded toggle as the hotkey."""
        try:
            self.svc.on_hotkey()
        except Exception as e:
            self.log.warning("HUD mic button: %s", e)

    def _toggle_privacy(self):
        """Hide or show the text; the level bar stays either way."""
        self.state["privacy"] = not bool(self.state.get("privacy"))
        self.log.info("HUD privacy: %s",
                      "on" if self.state["privacy"] else "off")

    # -- refresh loop ------------------------------------------------------
    def _tick(self):
        try:
            if self.state.get("open_settings"):
                self.state["open_settings"] = False
                self._open_settings_window()
            if self.state.get("open_onboarding"):
                self.state["open_onboarding"] = False
                self._open_onboarding_window()
            self._apply_alpha()
            st = dict(self.state)
            key = st.get("state", "idle")
            color = STATE_COLORS.get(key, "#2E86DE")
            self.lbl.configure(text=self._state_label(key), fg=color)
            if key == "recording":
                self.mic.configure(text="⏹", fg="#E74C3C")
            else:
                self.mic.configure(text="🎤", fg=FG)
            self._paint_dot(color)
            level = min(1.0, float(st.get("level", 0.0)) * 12.0)  # rms ~0..0.08
            self._paint_bar(level)
            hint = self.svc.tr("hud.mic_hint", device=st.get("device") or "")
            if (key == "recording" and float(st.get("level", 0)) < 0.003):
                hint += self.svc.tr("hud.silence_warn")
            self.hint.configure(text=hint)

            privacy = bool(st.get("privacy"))
            self.eye.configure(text="🙈" if privacy else "👁")
            partial = (self.svc.tr("hud.privacy_hint") if privacy
                       else (st.get("partial") or "…"))
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", partial)
            self.text.configure(state="disabled")
            self._autogrow(partial)

            last = st.get("last") or ""
            age = time.time() - float(st.get("last_ts") or 0)
            self.last.configure(text="" if privacy or age > LAST_TTL_S
                                else last)
        except Exception as e:
            self.log.warning("HUD tick: %s", e)
        if self.state.get("exit"):
            try:
                self.root.quit()
            except Exception:
                pass
            return
        self.root.after(120, self._tick)

    def _open_settings_window(self):
        tab = self.state.get("settings_tab")
        self.state["settings_tab"] = None
        try:
            alive = self._settings is not None and self._settings.win.winfo_exists()
        except Exception:
            alive = False
        if alive:
            self._settings.win.deiconify()
            if tab:
                self._settings.select_tab(tab)
            return
        import settings_window
        self._settings = settings_window.SettingsWindow(
            self.root, self.svc, self.log, tab=tab)

    def _open_onboarding_window(self):
        try:
            alive = (self._onboarding is not None
                     and self._onboarding.win.winfo_exists())
        except Exception:  # noqa: BLE001 - a closed window raises TclError
            alive = False
        if alive:
            self._onboarding.win.deiconify()
            return
        import onboarding
        self._onboarding = onboarding.OnboardingWindow(
            self.root, self.svc, self.log)

    def _apply_alpha(self):
        a = self.state.get("alpha")
        if a is not None:
            try:
                self.root.attributes("-alpha", max(0.3, min(1.0, float(a))))
            except Exception:
                pass

    def _autogrow(self, partial: str):
        """Grow the panel with the text when hud_autogrow is on."""
        try:
            if not self.cfg.get("hud_autogrow", True):
                want = BASE_TEXT_LINES
            else:
                cpl = 46  # chars per line at this width/font
                lines = sum(max(1, -(-len(l) // cpl)) for l in partial.split("\n"))
                want = max(BASE_TEXT_LINES, min(12, lines + 1))
            if want != int(self.text.cget("height")):
                self.text.configure(height=want)
                h = (self._root_h - BASE_TEXT_LINES * self._line_h
                     + want * self._line_h)
                if h != self._root_h:
                    self._root_h = h
                    x = self.root.winfo_x()
                    y = self.root.winfo_y()
                    self.root.geometry("%dx%d+%d+%d" % (self._W, h, x, y))
        except Exception:
            pass

    def _paint_dot(self, color):
        self.dot.delete("all")
        s = self._px
        self.dot.create_oval(s(2), s(2), s(12), s(12), fill=color, outline="")

    def _paint_bar(self, level):
        self.bar.delete("all")
        s = self._px
        self.bar.create_rectangle(0, 0, max(s(2), int((self._W - s(24)) * level)),
                                  s(10),
                                  fill="#3FA34D" if level < 0.9 else "#E74C3C",
                                  outline="")

    def run(self):
        self.root.mainloop()


def run_hud(svc, log):
    """Entry for the HUD thread: Tk must be created and used in one thread."""
    try:
        Hud(svc, log).run()
    except Exception as e:
        log.warning("HUD stopped: %s", e)
