"""Live monitoring panel (HUD): mic level, VAD, streaming partial text.

Tk runs in its own daemon thread; only that thread touches the widgets.
The state dict is written by service threads — single reads are GIL-safe.
The eye button switches the privacy mode: the text is hidden, the level
stays. The status line disappears LAST_TTL_S seconds after it was set.
The mic button records a phrase and leaves the text in the panel with the
Insert / Edit / Cancel row underneath, for the case when no text field was
focused before the click.
"""
import contextlib
import math
import os
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import winfx

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
ACCENT = "#2E86DE"

W = 380
BASE_TEXT_LINES = 4
DRAFT_MIN_LINES = 6   # a draft is read and edited, not glanced at: never a tiny panel
MAX_TEXT_LINES = 12
LINE_H = 20
BASE_H = 190
LAST_TTL_S = 30
ACTION_ROW_H = 68   # insert button + edit/cancel row, at 100% scaling

# Recognition overlay: the panel turns into a short "preloader" while the
# model turns speech into text. There is no measured percentage in the
# recognizer, so the counter is an animated estimate: it runs fast to ~90%
# within half a second, creeps to the cap, and only reaches 100 when the
# text is actually ready.
PROG_BG = "#04060A"
PROG_NUM = "#EEF2F7"
PROG_DIM = "#7A8290"
PROG_COOL = "#5BE9FF"
PROG_TRACK = "#20242B"
PROG_ALPHA = 0.92
PROG_TICK_MS = 40
PROG_RAMP_S = 0.16
PROG_CAP = 96.0
PROG_DONE_MS = 120
PROG_HOLD_MS = 140
PROG_FADE_MS = 170

# The file progress strip: a real bottom panel, not hint text. It shows one
# running file at a time, a moving bar while the queue has nothing in flight,
# and the batch counter underneath. A finished result holds for HOLD seconds.
FILE_PANEL_H = 46
FILE_PANEL_HOLD_S = 8.0
FILE_BAR_MS = 1200


def progress_percent(elapsed, ramp=PROG_RAMP_S, cap=PROG_CAP):
    """Animated recognition progress: fast at first, never 100 on its own."""
    if elapsed <= 0:
        return 0.0
    fast = 90.0 * (1.0 - math.exp(-elapsed / ramp))
    creep = (cap - 90.0) * (1.0 - math.exp(-elapsed / 1.1))
    return max(0.0, min(cap, fast + creep))


def wrapped_lines(text, cpl=46):
    """Wrapped line count at the HUD width; an empty line still takes one."""
    total = 0
    for line in (text or "").split("\n"):
        total += max(1, -(-len(line) // cpl))
    return total


def panel_lines(text, base=BASE_TEXT_LINES, cap=MAX_TEXT_LINES, cpl=46,
                minimum=BASE_TEXT_LINES):
    """Visible text lines to reserve: never below the base, never above the cap.

    ``minimum`` raises the floor for a draft: that text is shown to be read.
    """
    return max(base, minimum, min(cap, wrapped_lines(text, cpl)))


def panel_height(base_h, want, base=BASE_TEXT_LINES, line_h=LINE_H,
                 extra_h=0, minimum=BASE_TEXT_LINES):
    """Window height from the base, never from the previous height."""
    return base_h + extra_h + (max(want, minimum) - base) * line_h


def _file_fresh(finished, now, hold_s):
    """Whether a terminal file result is still inside its display hold."""
    try:
        return 0.0 <= now - float(finished or 0.0) < hold_s
    except (TypeError, ValueError):
        return False


def file_progress_view(st, now=None, hold_s=FILE_PANEL_HOLD_S) -> dict:
    """What the file strip shows; no Tk, no clock unless ``now`` is given.

    The sources are a strict table: a live batch, a live single file, a
    queued batch, a queued single file, a fresh batch result, a fresh single
    result. A batch child is never shown on its own: the batch carries the
    current name, so one panel speaks for the whole folder.
    """
    now = time.time() if now is None else now
    batch = st.get("file_batch") or {}
    job = st.get("file_job") or {}
    batch_id = str(batch.get("id") or "")
    job_id = str(job.get("id") or "")
    batch_state = str(batch.get("state") or "")
    job_state = str(job.get("state") or "")
    child = bool(job.get("batch_id"))
    terminal_states = ("done", "error", "cancelled")

    view = {
        "visible": False, "batch": False, "state": "", "title": "",
        "percent": 0, "overall_percent": 0, "overall_text": "",
        "complete": 0, "total": 0, "paused": False, "error": "",
        "indeterminate": False, "terminal": False, "failed": 0,
    }
    chosen = ""
    if batch_id and batch_state in ("running", "paused"):
        chosen = "batch"
    elif job_id and not child and job_state in ("running", "paused"):
        chosen = "single"
    elif batch_id and batch_state == "queued":
        chosen = "batch"
    elif job_id and not child and job_state == "queued":
        chosen = "single"
    elif (batch_id and batch_state in ("done", "cancelled")
          and _file_fresh(batch.get("finished_at"), now, hold_s)):
        chosen = "batch"
    elif (job_id and not child
          and job_state in ("done", "error", "cancelled")
          and _file_fresh(job.get("finished_at"), now, hold_s)):
        chosen = "single"
    if not chosen:
        return view
    if chosen == "batch":
        done = max(0, int(batch.get("done") or 0))
        skipped = max(0, int(batch.get("skipped") or 0))
        failed = max(0, int(batch.get("failed") or 0))
        cancelled = max(0, int(batch.get("cancelled") or 0))
        complete = done + skipped + failed + cancelled
        total = max(0, int(batch.get("total") or 0))
        state = batch_state
        if state == "done" and failed > 0:
            state = "error"
        current = str(batch.get("current") or "")
        percent = None
        if current:
            percent = int(round(max(0.0, min(
                100.0, float(batch.get("current_progress") or 0.0)))))
        view.update({
            "visible": True, "batch": True, "state": state,
            "title": str(batch.get("current_name") or ""),
            "percent": percent,
            "overall_percent": int(round(max(0.0, min(
                100.0, float(batch.get("progress") or 0.0))))),
            "overall_text": f"{complete}/{total}",
            "complete": complete, "total": total,
            "paused": state == "paused",
            "error": str(batch.get("error") or ""),
            "indeterminate": (state in ("queued", "running")
                              and (percent is None or percent == 0)),
            "terminal": state in terminal_states,
            "failed": failed,
        })
        return view
    terminal = job_state in terminal_states
    percent = int(round(max(0.0, min(
        100.0, float(job.get("progress") or 0.0)))))
    view.update({
        "visible": True, "batch": False, "state": job_state,
        "title": str(job.get("name")
                     or os.path.basename(str(job.get("source") or ""))),
        "percent": percent, "overall_percent": percent, "overall_text": "",
        "complete": 1 if terminal else 0, "total": 1,
        "paused": job_state == "paused",
        "error": str(job.get("error") or ""),
        "indeterminate": (job_state == "queued"
                          or (job_state == "running" and percent == 0)),
        "terminal": terminal,
        "failed": 1 if job_state == "error" else 0,
    })
    return view


def dpi_factor(win):
    """Windows scaling factor (1.0 at 100%, 1.5 at 150%) for fixed-pixel sizes."""
    try:
        return max(1.0, win.winfo_fpixels("1i") / 96.0)
    except Exception:  # noqa: BLE001 - the HUD thread must survive any Tk failure
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
        self._doctor = None
        self._root_h = BASE_H
        self._want = BASE_TEXT_LINES
        self._extra_h = 0
        self._file_h = 0
        self._actions_visible = False
        self._draft_rev = 0
        self._draft_active = False
        self._draft_edit = False
        self._draft_text = ""
        self._last_foreign_hwnd = None
        # recognition overlay state (created only while the model is busy)
        self._prog = None
        self._prog_canvas = None
        self._prog_job = None
        self._prog_t0 = 0.0
        self._prog_value = 0.0
        self._prog_closing = False

        self.root = tk.Tk()
        px = self._px = lambda n: int(round(n * dpi_factor(self.root)))
        self._W = px(W)
        self._line_h = px(LINE_H)
        base_h = px(BASE_H)
        self._base_h = self._root_h = base_h

        self.root.title("VibeForma")
        self.root.geometry("{}x{}+{}+{}".format(*self._place(base_h)))
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)   # borderless
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", float(self.cfg.get("hud_alpha", 0.94)))
        winfx.round_region(self.root, px(12))
        winfx.keep_round_region(self.root, px(12))

        # -- header: [⚙]  status …  |  [update] [✕] ------------------------
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=px(12), pady=(px(10), px(2)))
        gear = tk.Label(top, text="⚙", font=("Segoe UI", 12), fg=FG, bg=BG,
                        cursor="hand2")
        gear.pack(side="left")
        gear.bind("<Button-1>", lambda e: self._open_settings())
        self.eye = tk.Label(top, text="👁", font=("Segoe UI", 11), fg=FG,
                            bg=BG, cursor="hand2")
        self.eye.pack(side="left", padx=(px(4), 0))
        self.eye.bind("<Button-1>", lambda e: self._toggle_privacy())
        self.mic = tk.Label(top, text="🎤", font=("Segoe UI", 12), fg=FG,
                            bg=BG, cursor="hand2")
        self.mic.pack(side="left", padx=(px(4), 0))
        self.mic.bind("<Button-1>", lambda e: self._on_mic())
        self.dot = tk.Canvas(top, width=px(14), height=px(14), bg=BG,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(px(6), px(4)))
        self.lbl = tk.Label(top, text="…", font=("Segoe UI", 10, "bold"),
                            fg=FG, bg=BG, anchor="w")
        xlab = tk.Label(top, text="✕", font=("Segoe UI", 11), fg=DIM, bg=BG,
                        cursor="hand2")
        xlab.bind("<Button-1>", lambda e: self._hide_panel())
        sep = tk.Frame(top, width=px(1), height=px(16), bg="#3A4048")
        xlab.pack(side="right")
        sep.pack(side="right", padx=(px(4), px(8)))
        self.next_hist = tk.Label(top, text="›", font=("Segoe UI", 14),
                                  fg=FG, bg=BG, cursor="hand2", padx=px(3))
        self.next_hist.pack(side="right", padx=(0, px(6)))
        self.next_hist.bind("<Button-1>", lambda e: self._browse_history(1))
        self.prev_hist = tk.Label(top, text="‹", font=("Segoe UI", 14),
                                  fg=FG, bg=BG, cursor="hand2", padx=px(3))
        self.prev_hist.pack(side="right", padx=(px(6), px(2)))
        self.prev_hist.bind("<Button-1>", lambda e: self._browse_history(-1))
        # shown only while a newer release is pending; packed in _tick
        self.upd = tk.Label(top, text="", font=("Segoe UI", 9, "bold"),
                            fg="#FFFFFF", bg=ACCENT, cursor="hand2",
                            padx=px(10), pady=px(2))
        self.upd.bind("<Button-1>", lambda e: self._on_update())
        # the status label takes what is left, so the buttons never clip
        self.lbl.pack(side="left")
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
        # a closed window raises TclError while measuring the font
        with contextlib.suppress(tk.TclError):
            import tkinter.font as tkfont
            f = tkfont.Font(font=self.text.cget("font"))
            self._line_h = max(px(LINE_H), int(f.metrics("linespace")))

        self.last = tk.Label(self.root, text="", font=("Segoe UI", 8),
                             fg=DIM, bg=BG, anchor="w")
        self.last.pack(fill="x", padx=px(12), pady=(0, px(8)))
        self._bind_drag(self.last)

        # -- draft actions: shown under the panel after a panel dictation ----
        self.actions = tk.Frame(self.root, bg=BG)
        self.insert_btn = tk.Button(
            self.actions, text=self.svc.tr("draft.insert"),
            command=self._insert_draft, font=("Segoe UI", 9, "bold"),
            fg="#FFFFFF", bg=ACCENT, activebackground="#2472B8",
            activeforeground="#FFFFFF", relief="flat", bd=0,
            cursor="hand2", padx=px(8), pady=px(5))
        self.insert_btn.pack(fill="x", padx=px(12), pady=(0, px(4)))
        row = tk.Frame(self.actions, bg=BG)
        row.pack(fill="x", padx=px(12), pady=(0, px(8)))
        self.edit_btn = tk.Button(
            row, text=self.svc.tr("draft.edit"), command=self._toggle_edit,
            font=("Segoe UI", 9), fg=FG, bg="#2A2F38",
            activebackground="#3A4048", activeforeground="#FFFFFF",
            relief="flat", bd=0, cursor="hand2", padx=px(6), pady=px(5))
        self.edit_btn.pack(side="left")
        self.copy_btn = tk.Button(
            row, text=self.svc.tr("draft.copy"), command=self._copy_draft,
            font=("Segoe UI", 9), fg=FG, bg="#2A2F38",
            activebackground="#3A4048", activeforeground="#FFFFFF",
            relief="flat", bd=0, cursor="hand2", padx=px(6), pady=px(5))
        self.copy_btn.pack(side="left", padx=(px(6), 0))
        self.dict_btn = tk.Button(
            row, text=self.svc.tr("draft.to_dictionary"),
            command=self._dictionary_draft, font=("Segoe UI", 9), fg=FG,
            bg="#2A2F38", activebackground="#3A4048", activeforeground="#FFFFFF",
            relief="flat", bd=0, cursor="hand2", padx=px(6), pady=px(5))
        self.dict_btn.pack(side="left", padx=(px(6), 0))
        self.cancel_btn = tk.Button(
            row, text=self.svc.tr("draft.cancel"), command=self._cancel_draft,
            font=("Segoe UI", 9), fg=FG, bg="#2A2F38",
            activebackground="#3A4048", activeforeground="#FFFFFF",
            relief="flat", bd=0, cursor="hand2", padx=px(6), pady=px(5))
        self.cancel_btn.pack(side="right")
        for widget in (self.actions, self.insert_btn, self.edit_btn,
                       self.copy_btn, self.dict_btn, self.cancel_btn):
            widget.bind("<Enter>", lambda e: self._track_foreign_window(),
                        add="+")

        # -- file progress strip: the lowest line of the panel --------------
        # Packed on demand with side="bottom", so it always sits under the
        # text, the status line and the action row. takefocus=0 and no bind:
        # the hotkey, the clicks and the drag keep working as before.
        self.file_panel = tk.Frame(self.root, bg=BG, takefocus=0)
        row1 = tk.Frame(self.file_panel, bg=BG)
        row1.pack(fill="x")
        self.file_title = tk.Label(row1, text="", font=("Segoe UI", 8),
                                   fg=FG, bg=BG, anchor="w")
        self.file_title.pack(side="left")
        self.file_pct = tk.Label(row1, text="", font=("Consolas", 8),
                                 fg=DIM, bg=BG, anchor="e")
        self.file_pct.pack(side="right")
        self.file_bar = tk.Canvas(self.file_panel, height=self._px(4), bg=BG,
                                  highlightthickness=0, takefocus=0)
        self.file_bar.pack(fill="x")
        self.file_overall = tk.Label(self.file_panel, text="",
                                     font=("Consolas", 8), fg=DIM, bg=BG,
                                     anchor="w")
        self.file_overall.pack(fill="x")

        self.root.bind("<Escape>", self._on_escape)
        self.text.bind("<Control-Return>", self._on_ctrl_enter)
        self.root.after(120, self._tick)

    # -- layout / drag -----------------------------------------------------
    def _place(self, base_h):
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            # Reserve the tallest panel (text + action row): the top stays
            # put and the panel grows downward while the history is flipped.
            if self.cfg.get("hud_autogrow", True):
                max_h = base_h + (MAX_TEXT_LINES - BASE_TEXT_LINES) * self._line_h
            else:
                max_h = base_h
            max_h += self._px(ACTION_ROW_H) + self._px(FILE_PANEL_H)
            return self._W, base_h, sw - self._W - self._px(28), \
                max(self._px(28), sh - max_h - self._px(72))
        except Exception:  # noqa: BLE001 - the HUD thread must survive any Tk failure
            return self._W, base_h, 100, 100

    def _bind_drag(self, widget):
        widget.bind("<Button-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}"
                               f"+{e.y_root - self._drag[1]}")

    def _show_panel(self):
        with contextlib.suppress(Exception):
            self.root.deiconify()
            self.root.lift()

    def _hide_panel(self):
        """The close icon and Escape: the overlay must never outlive the panel."""
        self._destroy_progress()
        with contextlib.suppress(Exception):
            self.root.withdraw()

    def _open_settings(self):
        self.state["open_settings"] = True
        with contextlib.suppress(Exception):
            if not self.root.winfo_viewable():
                self.root.deiconify()

    def _state_label(self, key):
        label = self.svc.tr(f"states.{key}", hotkey=self.cfg.get("hotkey", ""))
        return key if label == f"states.{key}" else label

    def _on_update(self):
        """Install the pending release through the same bootstrap as README."""
        try:
            self.svc.apply_pending_update()
        except Exception as e:  # noqa: BLE001 - the HUD thread must survive any Tk failure
            self.log.warning("HUD update button: %s", e)

    def _sync_update_button(self):
        rel = self.svc.pending_update
        if rel:
            text = self.svc.tr("hud.update_to")
            if str(self.upd.cget("text")) != text:
                self.upd.configure(text=text)
            if not self.upd.winfo_ismapped():
                self.upd.pack(side="right", padx=(0, self._px(6)),
                              before=self.lbl)
        elif self.upd.winfo_ismapped():
            self.upd.pack_forget()

    def _toggle_privacy(self):
        """Hide or show the text; the level bar stays either way."""
        self.state["privacy"] = not bool(self.state.get("privacy"))
        self.log.info("HUD privacy: %s",
                      "on" if self.state["privacy"] else "off")

    # -- panel dictation ---------------------------------------------------
    def _on_mic(self):
        """Mic button: start a panel dictation or stop the running one."""
        with contextlib.suppress(Exception):
            # The panel has no title bar; force the focus so the click does
            # not leave the previous app as a silent paste target.
            self.root.focus_force()
        threading.Thread(target=self.svc.on_panel_mic, daemon=True).start()

    def _browse_history(self, step):
        """Arrows in the header: -1 older, +1 newer saved dictation."""
        with contextlib.suppress(Exception):
            self.root.focus_force()
        threading.Thread(target=self.svc.browse_history, args=(step,),
                         daemon=True).start()

    def _sync_mic(self, key):
        if key == "recording":
            text, color = "⏹", STATE_COLORS["recording"]
        elif key in ("processing", "loading", "updating"):
            text, color = "🎤", DIM
        else:
            text, color = "🎤", FG
        if str(self.mic.cget("text")) != text:
            self.mic.configure(text=text)
        if str(self.mic.cget("fg")) != color:
            self.mic.configure(fg=color)

    def _track_foreign_window(self):
        """Remember the last window that is not ours: the insert target."""
        try:
            import insert as insert_mod
            hwnd = insert_mod.get_foreground_hwnd()
            if hwnd and not insert_mod.is_own_window(hwnd):
                self._last_foreign_hwnd = hwnd
        except Exception as e:  # noqa: BLE001 - tracking must not stop the HUD
            self.log.debug("HUD foreground tracking: %s", e)

    def _on_ctrl_enter(self, _event=None):
        self._insert_draft()
        return "break"

    def _on_escape(self, _event=None):
        if self._draft_edit:
            self._save_edit()
        else:
            self._hide_panel()
        return "break"

    def _sync_draft(self, st):
        rev = int(st.get("draft_rev") or 0)
        if rev == self._draft_rev:
            return
        self._draft_rev = rev
        text = st.get("draft") or ""
        if text:
            self._show_draft(text)
        else:
            self._clear_draft()

    def _show_draft(self, text):
        """Show the recognized text read-only with the action row below."""
        self._draft_active = True
        self._draft_edit = False
        self._draft_text = text
        # The target is whatever the user focuses after the draft appears;
        # a window picked before it is not "the specified place".
        self._last_foreign_hwnd = None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")
        self._set_edit_label(False)
        self._set_actions_enabled(True)
        self._show_actions()
        self._autogrow(text, draft=True)

    def _clear_draft(self):
        self._draft_active = False
        self._draft_edit = False
        self._draft_text = ""
        self._last_foreign_hwnd = None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "…")
        self.text.configure(state="disabled")
        self._set_edit_label(False)
        self._hide_actions()
        self._autogrow("…")

    def _show_actions(self):
        if self._actions_visible:
            return
        self._actions_visible = True
        self._extra_h = self._px(ACTION_ROW_H)
        self.actions.pack(fill="x")
        self._apply_height()

    def _hide_actions(self):
        if not self._actions_visible:
            return
        self._actions_visible = False
        self._extra_h = 0
        self.actions.pack_forget()
        self._apply_height()

    def _set_actions_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for button in (self.insert_btn, self.edit_btn, self.copy_btn,
                       self.dict_btn, self.cancel_btn):
            button.configure(state=state)

    def _set_edit_label(self, editing):
        label = self.svc.tr("draft.done" if editing else "draft.edit")
        if str(self.edit_btn.cget("text")) != label:
            self.edit_btn.configure(text=label)

    def _toggle_edit(self):
        """Switch the draft between read-only and editable."""
        if not self._draft_active:
            return
        if self._draft_edit:
            self._save_edit()
            return
        self._draft_edit = True
        self.text.configure(state="normal")
        self._set_edit_label(True)
        with contextlib.suppress(Exception):
            self.root.focus_force()
            self.text.focus_set()
            self.text.mark_set("insert", "end-1c")
            self.text.see("end")

    def _save_edit(self):
        if self._draft_edit:
            self._draft_text = self._read_draft()
        self._draft_edit = False
        self.text.configure(state="disabled")
        self._set_edit_label(False)

    def _read_draft(self):
        try:
            return self.text.get("1.0", "end-1c")
        except Exception:  # noqa: BLE001 - the cached text stands
            return self._draft_text

    def _insert_draft(self):
        """Paste the reviewed text into the window the user picked last."""
        if not self._draft_active:
            return
        self._save_edit()
        text = self._draft_text.strip()
        if not text:
            self._cancel_draft()
            return
        self._set_actions_enabled(False)
        self._track_foreign_window()
        self.svc.insert_draft(text, self._last_foreign_hwnd)

    def _copy_draft(self):
        """Put the reviewed text into the clipboard and close the draft."""
        if not self._draft_active:
            return
        self._save_edit()
        text = self._draft_text.strip()
        if not text:
            self._cancel_draft()
            return
        self._set_actions_enabled(False)
        self.svc.copy_draft(text)

    def _dictionary_draft(self):
        """Offer the reviewed draft as a dictionary pair; the draft stays."""
        if not self._draft_active:
            return
        self._save_edit()
        text = self._draft_text.strip()
        if not text:
            return
        import dictlearn
        dictlearn.pair_dialog(self.root, self.svc.tr, text, self.svc.cfg)

    def _cancel_draft(self):
        if not self._draft_active:
            return
        self._clear_draft()
        self.svc.cancel_draft()

    # -- recognition overlay -----------------------------------------------
    def _start_progress(self):
        """Cover the panel with the fast percentage while the model works."""
        if self._prog is not None:
            self._destroy_progress()
        if not self.root.winfo_viewable():
            return
        try:
            ov = tk.Toplevel(self.root)
            ov.withdraw()
            ov.overrideredirect(True)
            ov.configure(bg=PROG_BG)
            ov.attributes("-topmost", True)
            ov.attributes("-alpha", PROG_ALPHA)
            cv = tk.Canvas(ov, bg=PROG_BG, highlightthickness=0)
            cv.pack(fill="both", expand=True)
            self._prog = ov
            self._prog_canvas = cv
            self._prog_num_font = tkfont.Font(family="Segoe UI Light", size=58)
            self._prog_pct_font = tkfont.Font(family="Consolas", size=11)
            self._prog_label_font = tkfont.Font(family="Consolas", size=8)
            cv.bind("<Configure>",
                    lambda e: self._paint_progress(self._prog_value))
            self._place_progress()
            ov.deiconify()
            ov.update_idletasks()
            winfx.round_region(ov, self._px(12))
            winfx.keep_round_region(ov, self._px(12))
            ov.lift()
            self._prog_t0 = time.time()
            self._prog_value = 0.0
            self._prog_closing = False
            self._paint_progress(0.0)
            self._prog_job = self.root.after(PROG_TICK_MS, self._tick_progress)
        except Exception as e:  # noqa: BLE001 - the HUD thread must survive any Tk failure
            self.log.warning("HUD recognition overlay: %s", e)
            self._destroy_progress()

    def _place_progress(self):
        """The overlay sits exactly on the panel, even after an autogrow."""
        if self._prog is None:
            return
        self.root.update_idletasks()
        x = self.root.winfo_rootx()
        y = self.root.winfo_rooty()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self._prog.geometry(f"{w}x{h}+{x}+{y}")

    def _paint_progress(self, value):
        cv = self._prog_canvas
        if cv is None:
            return
        cv.delete("all")
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w <= 1 or h <= 1:
            return
        px = self._px
        num = f"{max(0, min(100, int(round(value)))):02d}"
        left = px(18)
        baseline = h - px(34)
        # canvas text anchors sit on the box bottom, so the descent is added
        # to put the number, the percent sign and the label on one baseline
        cv.create_text(left, baseline + self._prog_num_font.metrics("descent"),
                       text=num, anchor="sw",
                       font=self._prog_num_font, fill=PROG_NUM)
        num_w = self._prog_num_font.measure(num)
        cv.create_text(left + num_w + px(3),
                       baseline + self._prog_pct_font.metrics("descent"),
                       text="%", anchor="sw",
                       font=self._prog_pct_font, fill=PROG_DIM)
        label = self.svc.tr("hud.recognizing")
        if label != "hud.recognizing":
            cv.create_text(w - left,
                           baseline + self._prog_label_font.metrics("descent"),
                           text=label, anchor="se",
                           font=self._prog_label_font, fill=PROG_DIM)
        # the running line: ice -> cyan, like the site preloader
        track_y = h - px(14)
        cv.create_rectangle(left, track_y, w - left, track_y + px(2),
                            fill=PROG_TRACK, outline="")
        fill = int((w - left * 2) * max(0.0, min(100.0, value)) / 100.0)
        seg = max(1, px(1))
        for i in range(0, fill, seg):
            t = i / max(1, fill)
            r = int(0xEE + (0x5B - 0xEE) * t)
            g = int(0xF2 + (0xE9 - 0xF2) * t)
            b = int(0xF7 + (0xFF - 0xF7) * t)
            cv.create_rectangle(left + i, track_y - px(1),
                                left + min(fill, i + seg), track_y + px(3),
                                fill=f"#{r:02x}{g:02x}{b:02x}", outline="")
        if fill > 0:
            cv.create_oval(left + fill - px(2), track_y - px(1),
                           left + fill + px(2), track_y + px(3),
                           fill=PROG_COOL, outline="")

    def _tick_progress(self):
        self._prog_job = None
        if self._prog is None or self._prog_closing:
            return
        if self.state.get("state") != "processing":
            self._finish_progress()
            return
        if not self.root.winfo_viewable():
            self._destroy_progress()
            return
        self._prog_value = progress_percent(time.time() - self._prog_t0)
        self._place_progress()
        self._paint_progress(self._prog_value)
        self._prog_job = self.root.after(PROG_TICK_MS, self._tick_progress)

    def _finish_progress(self):
        """The text is ready: jump to 100, hold, then fade the overlay out."""
        if self._prog is None or self._prog_closing:
            return
        self._prog_closing = True
        if self._prog_job is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._prog_job)
            self._prog_job = None
        start = self._prog_value
        steps = max(1, PROG_DONE_MS // PROG_TICK_MS)
        self._prog_step = 0
        self._prog_from = start
        self._prog_steps = steps
        self._finish_progress_step()

    def _finish_progress_step(self):
        if self._prog is None:
            return
        self._prog_step += 1
        value = self._prog_from + (100.0 - self._prog_from) * min(
            1.0, self._prog_step / self._prog_steps)
        self._prog_value = value
        self._place_progress()
        self._paint_progress(value)
        if self._prog_step < self._prog_steps:
            self._prog_job = self.root.after(PROG_TICK_MS,
                                             self._finish_progress_step)
        else:
            self._prog_job = self.root.after(PROG_HOLD_MS, self._fade_progress)

    def _fade_progress(self, step=0):
        if self._prog is None:
            return
        self._place_progress()
        alpha = PROG_ALPHA * (1.0 - step * PROG_TICK_MS / PROG_FADE_MS)
        if alpha <= 0.02:
            self._destroy_progress()
            return
        with contextlib.suppress(Exception):
            self._prog.attributes("-alpha", alpha)
        self._prog_job = self.root.after(PROG_TICK_MS,
                                         self._fade_progress, step + 1)

    def _destroy_progress(self):
        if self._prog_job is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._prog_job)
            self._prog_job = None
        prog, self._prog = self._prog, None
        self._prog_canvas = None
        self._prog_closing = False
        self._prog_value = 0.0
        if prog is not None:
            with contextlib.suppress(Exception):
                prog.destroy()

    # -- file progress strip -----------------------------------------------
    def _sync_file_panel(self, st):
        """Pack or hide the strip and keep its three lines current."""
        with contextlib.suppress(Exception):
            view = file_progress_view(st)
            if not view["visible"]:
                if self._file_h:
                    self.file_panel.pack_forget()
                    self._file_h = 0
                    self._apply_height()
                return
            if self._file_h == 0:
                self.file_panel.pack(side="bottom", fill="x",
                                     padx=self._px(12),
                                     pady=(0, self._px(8)))
                self._file_h = self._px(FILE_PANEL_H)
                self._apply_height()
            state_word = self.svc.tr("hud.file_state_" + view["state"])
            title = self.svc.tr("hud.file_panel_title", state=state_word,
                                name=view["title"] or "\u2026")
            if str(self.file_title.cget("text")) != title:
                self.file_title.configure(text=title)
            percent = "" if view["percent"] is None else f"{view['percent']}%"
            if str(self.file_pct.cget("text")) != percent:
                self.file_pct.configure(text=percent)
            overall = ""
            if view["batch"]:
                overall = self.svc.tr("hud.file_batch", done=view["complete"],
                                      total=view["total"],
                                      pct=view["overall_percent"],
                                      failed=view["failed"])
            if view["error"]:
                failure = self.svc.tr("notify.file_failed",
                                      error=view["error"])
                overall = f"{overall} \u00b7 {failure}" if overall else failure
            color = STATE_COLORS["error"] if view["error"] else DIM
            if str(self.file_overall.cget("text")) != overall:
                self.file_overall.configure(text=overall)
            if str(self.file_overall.cget("fg")) != color:
                self.file_overall.configure(fg=color)
            self._paint_file_bar(view)

    def _paint_file_bar(self, view):
        """Redraw the strip every tick: a sweeping segment or a real percent."""
        bar = self.file_bar
        bar.delete("all")
        width = bar.winfo_width()
        if width <= 1:
            return
        height = max(1, bar.winfo_height())
        bar.create_rectangle(0, 0, width, height, fill=PROG_TRACK, outline="")
        state = view["state"]
        color = PROG_COOL if state in ("queued", "running", "done") else "#5D6D7E"
        if state == "error":
            color = STATE_COLORS["error"]
        if view["indeterminate"]:
            segment = max(self._px(28), width // 5)
            phase = (time.time() * 1000.0 % FILE_BAR_MS) / FILE_BAR_MS
            start = phase * (width + segment) - segment
            left = max(0.0, min(float(width), start))
            right = max(0.0, min(float(width), start + segment))
            if right > left:
                bar.create_rectangle(left, 0, right, height, fill=color,
                                     outline="")
            return
        percent = view["percent"]
        if percent is None:
            percent = view["overall_percent"]
        fill = width * max(0.0, min(100.0, float(percent))) / 100.0
        if fill > 0:
            bar.create_rectangle(0, 0, fill, height, fill=color, outline="")

    # -- refresh loop ------------------------------------------------------
    def _tick(self):
        try:
            if self.svc.panel_request.is_set():
                self.svc.panel_request.clear()
                self._show_panel()
            if self.state.get("open_settings"):
                self.state["open_settings"] = False
                self._open_settings_window()
            if self.state.get("open_doctor"):
                self.state["open_doctor"] = False
                self._open_doctor_window()
            if self.state.get("open_onboarding"):
                self.state["open_onboarding"] = False
                self._open_onboarding_window()
            self._apply_alpha()
            st = dict(self.state)
            self._sync_file_panel(st)
            key = st.get("state", "idle")
            color = STATE_COLORS.get(key, "#2E86DE")
            self.lbl.configure(text=self._state_label(key), fg=color)
            self._sync_update_button()
            self._sync_mic(key)
            if key == "processing":
                if self._prog is None or self._prog_closing:
                    self._start_progress()
            elif self._prog is not None and not self._prog_closing:
                self._finish_progress()
            self._track_foreign_window()
            self._sync_draft(st)
            self._paint_dot(color)
            level = min(1.0, float(st.get("level", 0.0)) * 12.0)  # rms ~0..0.08
            self._paint_bar(level)
            hint = self.svc.tr("hud.mic_hint", device=st.get("device") or "")
            if (key == "recording" and float(st.get("level", 0)) < 0.003):
                hint += self.svc.tr("hud.silence_warn")
            # The file progress lives in the strip below, never in this hint:
            # the two percentages must not be read as one number.
            self.hint.configure(text=hint)

            privacy = bool(st.get("privacy"))
            self.eye.configure(text="🙈" if privacy else "👁")
            if not self._draft_active:
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
        except Exception as e:  # noqa: BLE001 - the HUD thread must survive any Tk failure
            self.log.warning("HUD tick: %s", e)
        if self.state.get("exit"):
            with contextlib.suppress(Exception):
                self.root.quit()
            return
        self.root.after(120, self._tick)

    def _open_settings_window(self):
        tab = self.state.get("settings_tab")
        self.state["settings_tab"] = None
        try:
            alive = self._settings is not None and self._settings.win.winfo_exists()
        except Exception:  # noqa: BLE001 - the HUD thread must survive any Tk failure
            alive = False
        if alive:
            with contextlib.suppress(Exception):
                # The settings table needs the right part of the screen;
                # a topmost panel would cover it.
                self.root.attributes("-topmost", False)
            self._settings.win.deiconify()
            self._settings.win.lift()
            if tab:
                self._settings.select_tab(tab)
            return
        import settings_window
        with contextlib.suppress(Exception):
            self.root.attributes("-topmost", False)
        self._settings = settings_window.SettingsWindow(
            self.root, self.svc, self.log, tab=tab)
        self._settings.win.bind("<Destroy>", self._settings_closed, add="+")

    def _settings_closed(self, event=None):
        if (event is not None
                and event.widget is not getattr(self._settings, "win", None)):
            return
        with contextlib.suppress(Exception):
            self.root.attributes("-topmost", True)

    def _open_doctor_window(self):
        try:
            alive = (self._doctor is not None
                     and self._doctor.winfo_exists())
        except Exception:  # noqa: BLE001 - the HUD thread must survive any Tk failure
            alive = False
        if alive:
            self._doctor.deiconify()
            return
        import doctor
        self._doctor = doctor.show_report(self.root, self.svc.tr, self.log)

    def _open_onboarding_window(self):
        try:
            alive = (self._onboarding is not None
                     and self._onboarding.win.winfo_exists())
        except Exception:  # noqa: BLE001 - the HUD thread must survive any Tk failure
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
            with contextlib.suppress(Exception):
                self.root.attributes("-alpha", max(0.3, min(1.0, float(a))))

    def _display_lines(self):
        """Wrapped lines the Text actually shows; 0 when Tk cannot tell."""
        try:
            shown = self.text.count("1.0", "end-1c", "displaylines")
            if isinstance(shown, (tuple, list)):
                shown = shown[0]
            return int(shown or 0)
        except Exception:  # noqa: BLE001 - the char estimate stands
            return 0

    def _autogrow(self, text: str, draft: bool = False):
        """Grow the panel with the text, keeping its bottom edge in place.

        A draft panel reserves DRAFT_MIN_LINES: the text is there to be read.
        """
        with contextlib.suppress(Exception):
            minimum = DRAFT_MIN_LINES if draft else BASE_TEXT_LINES
            if not self.cfg.get("hud_autogrow", True):
                want = minimum
            else:
                want = panel_lines(text, minimum=minimum)
                shown = self._display_lines()
                if shown:
                    want = max(minimum, min(MAX_TEXT_LINES, shown))
            self._want = want
            if want != int(self.text.cget("height")):
                self.text.configure(height=want)
            self._apply_height()

    def _apply_height(self):
        """Height from the text lines and the visible extra rows; top stays put."""
        with contextlib.suppress(Exception):
            minimum = DRAFT_MIN_LINES if self._draft_active else BASE_TEXT_LINES
            extra = self._extra_h + self._file_h
            h = panel_height(self._base_h + extra, self._want,
                             base=BASE_TEXT_LINES, line_h=self._line_h,
                             minimum=minimum)
            screen_h = self.root.winfo_screenheight()
            available = max(self._base_h + extra,
                            screen_h - self.root.winfo_y() - self._px(28))
            h = min(h, available)
            if h != self._root_h:
                # Apply the previous geometry first: two size changes in one
                # tick would read stale coordinates and shift the panel.
                self.root.update_idletasks()
                x = self.root.winfo_x()
                y = self.root.winfo_y()
                self._root_h = h
                self.root.geometry(f"{self._W}x{h}+{x}+{y}")

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
    except Exception as e:  # noqa: BLE001 - the HUD thread must survive any Tk failure
        log.warning("HUD stopped: %s", e)
