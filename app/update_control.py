"""Always-visible update check in the settings header.

The header carries the running version and a check button; the result of the
last check is a slim row directly under the header, above the search field.
The control only reads ``svc.update_check_state``/``svc.update_check_tag``
and calls the service's three update methods, so a live Tk test can drive it
with a fake service without importing ``settings_window`` (that module loads
WinDLL at import time).
"""
from __future__ import annotations

import contextlib
import tkinter as tk

POLL_MS = 500

IDLE = "idle"
CHECKING = "checking"
LATEST = "latest"
FOUND = "found"
APPLYING = "applying"
DEFERRED = "deferred"
SOURCE_CHECKOUT = "source_checkout"
ERROR = "error"
INSTALL_FAILED = "install_failed"

GREEN = "#9FE0A8"
RED = "#E06C75"


class UpdateControl:
    """The check button in the header and its status row under it."""

    def __init__(self, header, *, svc, tr, px, version, bg, fg, dim,
                 field_bg, accent, before=None):
        self.svc = svc
        self.tr = tr
        self.version = version
        self._px = px
        self._fg = fg
        self._dim = dim
        self._before = before
        self._state = ""
        self._tag = ""
        self._row_shown = False

        self.button = tk.Button(
            header, text=tr("updates.check"), command=self.check,
            bg=field_bg, fg=fg, relief="flat", activebackground="#2A2F38",
            activeforeground=fg, disabledforeground=dim,
            font=("Segoe UI", 9), padx=px(10), pady=px(2), cursor="hand2")
        self.button.pack(side="left", padx=(px(8), 0))

        self.row = tk.Frame(header.master, bg=bg)
        self.status = tk.Label(self.row, text="", font=("Segoe UI", 9),
                               fg=fg, bg=bg, anchor="w")
        self.status.pack(side="left")
        self.download = tk.Button(
            self.row, text=tr("updates.download"), command=self._download,
            bg=accent, fg="white", relief="flat", activebackground="#1B6EC2",
            activeforeground="white", font=("Segoe UI", 9, "bold"),
            padx=px(10), pady=px(1), cursor="hand2")
        self.later = tk.Button(
            self.row, text=tr("updates.later"), command=self._later,
            bg=field_bg, fg=fg, relief="flat", activebackground="#2A2F38",
            activeforeground=fg, font=("Segoe UI", 9), padx=px(10),
            pady=px(1), cursor="hand2")

        self.refresh()
        self.row.after(POLL_MS, self._poll)

    # -- actions ---------------------------------------------------------
    def check(self) -> None:
        """Ask the service for a check; it sets ``checking`` synchronously."""
        with contextlib.suppress(Exception):
            self.svc.check_updates_now()
        self.refresh()

    def _download(self) -> None:
        with contextlib.suppress(Exception):
            self.svc.apply_pending_update()
        self.refresh()

    def _later(self) -> None:
        with contextlib.suppress(Exception):
            self.svc.dismiss_pending_update()
        self.refresh()

    # -- state -----------------------------------------------------------
    def refresh(self) -> None:
        """Read the service state; repaint only when it changed."""
        state = str(getattr(self.svc, "update_check_state", IDLE) or IDLE)
        tag = str(getattr(self.svc, "update_check_tag", "") or "")
        if state == self._state and tag == self._tag:
            return
        self._state = state
        self._tag = tag or self.version
        self._paint(state, self._tag)

    def _paint(self, state: str, tag: str) -> None:
        if state == IDLE:
            self._hide_row()
            self._button_idle()
            return
        self._show_row()
        text, color, actions = self._describe(state, tag)
        self.status.configure(text=text, fg=color)
        if state == CHECKING:
            self.button.configure(state="disabled",
                                  text=self.tr("updates.checking"))
        elif state == APPLYING:
            self.button.configure(state="disabled",
                                  text=self.tr("updates.check"))
        else:
            self._button_idle()
        if actions:
            self._show_actions()
        else:
            self._hide_actions()

    def _describe(self, state: str, tag: str) -> tuple[str, str, bool]:
        if state == CHECKING:
            return self.tr("updates.checking"), self._dim, False
        if state == LATEST:
            return self.tr("updates.latest", ver=tag), GREEN, False
        if state == FOUND:
            return self.tr("updates.available", tag=tag), self._fg, True
        if state == APPLYING:
            return self.tr("updates.applying", tag=tag), self._dim, False
        if state == DEFERRED:
            return self.tr("updates.deferred"), self._fg, True
        if state == SOURCE_CHECKOUT:
            return self.tr("updates.source_checkout"), self._dim, False
        if state == INSTALL_FAILED:
            return self.tr("updates.install_failed", tag=tag), RED, False
        return self.tr("updates.failed"), RED, False

    def _button_idle(self) -> None:
        self.button.configure(state="normal", text=self.tr("updates.check"))

    # -- widgets ---------------------------------------------------------
    def _show_row(self) -> None:
        if self._row_shown:
            return
        options = {"fill": "x", "padx": self._px(18),
                   "pady": (self._px(4), 0)}
        if self._before is None:
            self.row.pack(**options)
        else:
            self.row.pack(before=self._before, **options)
        self._row_shown = True

    def _hide_row(self) -> None:
        if not self._row_shown:
            return
        self.row.pack_forget()
        self._row_shown = False

    def _show_actions(self) -> None:
        if not self.download.winfo_manager():
            self.download.pack(side="right", padx=(self._px(6), 0))
        if not self.later.winfo_manager():
            self.later.pack(side="right", padx=(self._px(6), 0))

    def _hide_actions(self) -> None:
        if self.download.winfo_manager():
            self.download.pack_forget()
        if self.later.winfo_manager():
            self.later.pack_forget()

    def _poll(self) -> None:
        """Poll the service state on the Tk thread; never blocks."""
        try:
            if not self.row.winfo_exists():
                return
        except tk.TclError:
            return
        with contextlib.suppress(tk.TclError):
            self.refresh()
        try:
            self.row.after(POLL_MS, self._poll)
        except tk.TclError:
            return
