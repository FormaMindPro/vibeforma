"""First-run onboarding: language, microphone, hotkey.

Pure helpers (hotkey parsing and validation, safe defaults, the completed
flag) import no tkinter and no pynput at module load, so tests run without
a GUI or a keyboard backend. The wizard itself is a Toplevel on the HUD
thread; it only polls the shared live view.
"""
import contextlib

MODIFIERS = frozenset({"ctrl", "alt", "shift", "win", "cmd", "super"})
MODIFIER_ORDER = ("ctrl", "alt", "shift", "win", "cmd", "super")
RESERVED = frozenset({
    "ctrl+alt+delete", "alt+f4", "alt+tab", "ctrl+esc",
    "ctrl+shift+esc", "win+l", "win+d", "win+e",
})
DEFAULT_HOTKEY = "ctrl+alt+space"
TEST_SECONDS = 4.0


def hotkey_tokens(combo):
    """'Ctrl+Alt+Space' -> ['ctrl', 'alt', 'space']."""
    return [t.strip().lower() for t in str(combo or "").split("+") if t.strip()]


def normalize_hotkey(combo):
    """Lowercase, drop duplicates, modifiers first, key last."""
    mods, keys = [], []
    for token in hotkey_tokens(combo):
        bucket = mods if token in MODIFIERS else keys
        if token not in bucket:
            bucket.append(token)
    mods.sort(key=lambda t: MODIFIER_ORDER.index(t)
              if t in MODIFIER_ORDER else len(MODIFIER_ORDER))
    return "+".join(mods + keys)


def combo_to_pynput(combo):
    """'ctrl+alt+space' -> '<ctrl>+<alt>+<space>' (same shape pynput takes)."""
    return "+".join(f"<{t}>" for t in hotkey_tokens(combo))


def reserved_conflict(combo):
    """The matching reserved combination, or '' when there is none."""
    norm = normalize_hotkey(combo)
    return norm if norm in RESERVED else ""


def validate_hotkey(combo):
    """(ok, code, warning) for a hotkey combination.

    ok=False blocks saving. A reserved combination is a warning, not a
    refusal: it may still work depending on the system and the app.
    """
    tokens = hotkey_tokens(combo)
    if not tokens:
        return False, "empty", ""
    keys = [t for t in tokens if t not in MODIFIERS]
    mods = [t for t in tokens if t in MODIFIERS]
    if not keys and mods:
        return False, "modifiers_only", ""
    if len(keys) > 1:
        return False, "invalid", ""
    if tokens.count(keys[0]) > 1:
        return False, "invalid", ""
    try:
        from pynput import keyboard
    except ImportError:
        return True, "ok", ("conflict" if reserved_conflict(combo) else "")
    try:
        keyboard.HotKey.parse(combo_to_pynput(normalize_hotkey(combo)))
    except Exception:  # noqa: BLE001 - any backend refusal means invalid
        return False, "invalid", ""
    return True, "ok", ("conflict" if reserved_conflict(combo) else "")


def needed(cfg):
    """True on the first run, until the wizard is finished or skipped."""
    return not bool((cfg or {}).get("onboarding_done", False))


def apply_choices(cfg, language=None, locale=None, hotkey=None,
                  input_device=None, done=True):
    """Merge the wizard's answers into safe values.

    An invalid hotkey falls back to the current valid one or the default;
    an unknown language falls back to ``en``. Returns a new dict.
    """
    from config import LANGS

    out = dict(cfg or {})
    lang = str(language or out.get("language") or "en")
    out["language"] = lang if lang in LANGS else "en"
    if locale:
        out["locale"] = str(locale)
    chosen = hotkey if hotkey is not None else out.get("hotkey")
    ok, _code, _warn = validate_hotkey(chosen or "")
    if not ok:
        current = out.get("hotkey") or ""
        ok_current, _c, _w = validate_hotkey(current)
        chosen = current if ok_current else DEFAULT_HOTKEY
    out["hotkey"] = normalize_hotkey(chosen)
    if input_device is not None:
        out["input_device"] = str(input_device or "")
    if done:
        out["onboarding_done"] = True
    return out


class OnboardingWindow:
    """Three-step wizard on the HUD thread; polls service.live."""

    def __init__(self, master, service, log):
        import tkinter as tk

        self.tk = tk
        self.svc = service
        self.log = log
        self.tr = service.tr
        self.live = service.live
        self.cfg = service.cfg
        self.step = 0
        self.choices = {
            "language": self.cfg.get("language", "en"),
            "locale": self.cfg.get("locale", "auto"),
            "hotkey": self.cfg.get("hotkey", DEFAULT_HOTKEY),
            "input_device": self.cfg.get("input_device", ""),
        }
        self._capturing = False
        self._pressed = []
        self._closed = False

        self.win = tk.Toplevel(master)
        self.win.title(self.tr("onboarding.title"))
        self.win.configure(bg="#16181D")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._skip)

        tk.Label(self.win, text=self.tr("onboarding.title"),
                 font=("Segoe UI", 12, "bold"), fg="#8FB8E8",
                 bg="#16181D").pack(anchor="w", padx=18, pady=(14, 2))
        self.head = tk.Label(self.win, text="", font=("Segoe UI", 10, "bold"),
                             fg="#D5D9E0", bg="#16181D")
        self.head.pack(anchor="w", padx=18)
        self.body = tk.Frame(self.win, bg="#16181D")
        self.body.pack(fill="both", expand=True, padx=18, pady=(8, 4))
        self.note = tk.Label(self.win, text="", font=("Segoe UI", 9),
                             fg="#9AA3B0", bg="#16181D", justify="left",
                             wraplength=420)
        self.note.pack(anchor="w", padx=18, pady=(0, 6))

        bar = tk.Frame(self.win, bg="#16181D")
        bar.pack(fill="x", padx=18, pady=(0, 14))
        self.btn_skip = tk.Button(bar, text=self.tr("onboarding.skip"),
                                  command=self._skip, width=10, relief="flat",
                                  bg="#22262E", fg="#D5D9E0",
                                  activebackground="#2A2F38")
        self.btn_skip.pack(side="left")
        self.btn_next = tk.Button(bar, text=self.tr("onboarding.next"),
                                  command=self._next, width=10, relief="flat",
                                  bg="#2E86DE", fg="white",
                                  activebackground="#1B6EC2")
        self.btn_next.pack(side="right")
        self.btn_back = tk.Button(bar, text=self.tr("onboarding.back"),
                                  command=self._back, width=10, relief="flat",
                                  bg="#22262E", fg="#D5D9E0",
                                  activebackground="#2A2F38")
        self.btn_back.pack(side="right", padx=(0, 6))

        self._render()
        self.win.after(120, self._tick)

    # -- steps -----------------------------------------------------------
    def _render(self):
        tk = self.tk
        for child in self.body.winfo_children():
            child.destroy()
        self.note.configure(text="")
        self.head.configure(text=(
            f"{self.tr('onboarding.step')} {self.step + 1}/3  -  "
            f"{self.tr(f'onboarding.step{self.step + 1}')}"))
        builder = (self._build_language, self._build_microphone,
                   self._build_hotkey)[self.step]
        builder(tk)
        self.btn_back.configure(state="normal" if self.step > 0 else "disabled")
        self.btn_next.configure(
            text=self.tr("onboarding.finish") if self.step == 2
            else self.tr("onboarding.next"))

    def _build_language(self, tk):
        import i18n
        from config import LANG_NAMES, LANGS

        tk.Label(self.body, text=self.tr("settings.recog_lang"),
                 bg="#16181D", fg="#D5D9E0").pack(anchor="w", pady=(2, 0))
        self._lang_by_name = {LANG_NAMES.get(c, c): c for c in LANGS}
        current = LANG_NAMES.get(self.choices["language"], self.choices["language"])
        self.v_lang = tk.StringVar(value=current)
        tk.OptionMenu(self.body, self.v_lang, *self._lang_by_name,
                      command=self._pick_language).pack(anchor="w", pady=(0, 8))

        tk.Label(self.body, text=self.tr("settings.ui_lang"),
                 bg="#16181D", fg="#D5D9E0").pack(anchor="w")
        self._ui_by_name = {"auto": "auto"}
        for code in i18n.available_locales():
            self._ui_by_name[LANG_NAMES.get(code, code)] = code
        cur = self.choices.get("locale", "auto")
        self.v_ui = tk.StringVar(
            value=next((name for name, code in self._ui_by_name.items()
                        if code == cur), "auto"))
        tk.OptionMenu(self.body, self.v_ui, *self._ui_by_name,
                      command=lambda _v: self.choices.update(
                          locale=self._ui_by_name.get(self.v_ui.get(), "auto"))
                      ).pack(anchor="w")
        self.note.configure(text=self.tr("onboarding.step1_note"))

    def _pick_language(self, _value):
        self.choices["language"] = self._lang_by_name.get(
            self.v_lang.get(), self.choices["language"])

    def _build_microphone(self, tk):
        names = [""] + (self.svc.input_device_names() or [])
        current = self.choices.get("input_device", "")
        self.v_dev = tk.StringVar(value=current)
        tk.Label(self.body, text=self.tr("settings.device"),
                 bg="#16181D", fg="#D5D9E0").pack(anchor="w", pady=(2, 0))
        tk.OptionMenu(self.body, self.v_dev, *names,
                      command=lambda _v: self.choices.update(
                          input_device=self.v_dev.get())
                      ).pack(anchor="w", pady=(0, 8))
        self.v_level = tk.StringVar(value=self.tr("onboarding.level", pct=0))
        tk.Label(self.body, textvariable=self.v_level, bg="#16181D",
                 fg="#9FE0A8").pack(anchor="w")
        self.btn_test = tk.Button(self.body, text=self.tr("onboarding.test"),
                                  command=self._toggle_test, width=18,
                                  relief="flat", bg="#22262E", fg="#D5D9E0",
                                  activebackground="#2A2F38")
        self.btn_test.pack(anchor="w", pady=(6, 6))
        self.test_lbl = tk.Label(self.body, text="", bg="#16181D",
                                 fg="#D5D9E0", wraplength=420, justify="left")
        self.test_lbl.pack(anchor="w")
        self.note.configure(text=self.tr("onboarding.test_hint"))

    def _build_hotkey(self, tk):
        self.v_hotkey = tk.StringVar(value=self.choices.get("hotkey", ""))
        tk.Label(self.body, text=self.tr("settings.hotkey"), bg="#16181D",
                 fg="#D5D9E0").pack(anchor="w", pady=(2, 0))
        tk.Entry(self.body, textvariable=self.v_hotkey, width=26,
                 bg="#22262E", fg="#D5D9E0",
                 insertbackground="#D5D9E0").pack(anchor="w")
        self.btn_record = tk.Button(
            self.body, text=self.tr("onboarding.record"),
            command=self._capture, width=18, relief="flat",
            bg="#22262E", fg="#D5D9E0", activebackground="#2A2F38")
        self.btn_record.pack(anchor="w", pady=6)
        self.hot_lbl = tk.Label(self.body, text="", bg="#16181D",
                                fg="#9AA3B0", wraplength=420, justify="left")
        self.hot_lbl.pack(anchor="w")
        self.note.configure(text=self.tr("onboarding.hotkey_note"))

    # -- microphone test --------------------------------------------------
    def _toggle_test(self):
        if self.live.get("test_state") == "recording":
            self.svc.stop_mic_test()
            return
        if self.svc.start_mic_test():
            self.btn_test.configure(text=self.tr("onboarding.stop"))
            self.test_lbl.configure(text=self.tr("onboarding.test_recording"))
        else:
            self.test_lbl.configure(text=self.tr("onboarding.test_wait"))

    def _show_test(self):
        state = self.live.get("test_state") or ""
        if state == "recording":
            self.v_level.set(self.tr("onboarding.level",
                                     pct=int(round(100 * float(
                                         self.live.get("level") or 0)))))
            return
        self.v_level.set(self.tr("onboarding.level", pct=0))
        self.btn_test.configure(text=self.tr("onboarding.test"))
        if state == "done":
            self.test_lbl.configure(text=self.live.get("test_text") or "")
        elif state == "empty":
            self.test_lbl.configure(text=self.tr("onboarding.test_empty"))
        elif state == "error":
            self.test_lbl.configure(text=self.tr("onboarding.test_failed"))

    # -- hotkey capture ---------------------------------------------------
    def _capture(self):
        self._capturing = True
        self._pressed = []
        self.hot_lbl.configure(text=self.tr("onboarding.capture"))
        self.win.bind("<KeyPress>", self._on_key)
        self.win.bind("<KeyRelease>", self._on_release)
        self.win.focus_force()

    def _key_name(self, event):
        name = str(event.keysym).lower()
        return {"control_l": "ctrl", "control_r": "ctrl",
                "alt_l": "alt", "alt_r": "alt",
                "shift_l": "shift", "shift_r": "shift",
                "super_l": "win", "super_r": "win",
                "return": "enter"}.get(name, name)

    def _on_key(self, event):
        if not self._capturing:
            return
        name = self._key_name(event)
        if name == "escape":
            self._capturing = False
            self.hot_lbl.configure(text="")
            return
        if name not in self._pressed:
            self._pressed.append(name)
        if name in MODIFIERS:
            return
        combo = "+".join(self._mods() + [name])
        self._capturing = False
        self._apply_hotkey(combo)

    def _on_release(self, event):
        name = self._key_name(event)
        if name in self._pressed:
            self._pressed.remove(name)

    def _mods(self):
        return [m for m in MODIFIER_ORDER if m in self._pressed]

    def _apply_hotkey(self, combo):
        ok, code, warning = validate_hotkey(combo)
        if not ok:
            self.hot_lbl.configure(text=self.tr("onboarding.hotkey_" + code))
            return
        self.choices["hotkey"] = normalize_hotkey(combo)
        self.v_hotkey.set(self.choices["hotkey"])
        if warning == "conflict":
            self.hot_lbl.configure(
                text=self.tr("onboarding.hotkey_conflict"))
        else:
            self.hot_lbl.configure(text=self.tr(
                "onboarding.hotkey_ok", combo=self.choices["hotkey"]))

    # -- navigation -------------------------------------------------------
    def _back(self):
        if self.step > 0:
            self.step -= 1
            self._render()

    def _next(self):
        if self.step < 2:
            self.step += 1
            self._render()
            return
        typed = self.v_hotkey.get().strip() if hasattr(self, "v_hotkey") else ""
        candidate = typed or self.choices.get("hotkey") or ""
        ok, code, _warning = validate_hotkey(candidate)
        if not ok:
            self.hot_lbl.configure(text=self.tr("onboarding.hotkey_" + code))
            return
        self.choices["hotkey"] = normalize_hotkey(candidate)
        self._finish()

    def _finish(self):
        self.svc.finish_onboarding(dict(self.choices))
        self._close()

    def _skip(self):
        self.svc.finish_onboarding({})
        self._close()

    def _close(self):
        self._closed = True
        if self.live.get("test_state") == "recording":
            self.svc.stop_mic_test()
        with contextlib.suppress(Exception):
            self.win.destroy()

    # -- polling ----------------------------------------------------------
    def _tick(self):
        if self._closed:
            return
        try:
            if not self.win.winfo_exists():
                return
            if self.step == 1:
                self._show_test()
        except Exception as e:  # noqa: BLE001 - a closed window raises TclError
            self.log.warning("Onboarding tick: %s", e)
        self.win.after(120, self._tick)
