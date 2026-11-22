"""Tray icon and menu (pystray + Pillow)."""
import contextlib
import logging
import os

import pystray
import updater
from config import LANG_NAMES, MODELS
from PIL import Image, ImageDraw

STATE_COLORS = {
    "loading": "#F39C12",
    "idle": "#2E86DE",
    "recording": "#E74C3C",
    "processing": "#F39C12",
    "paused": "#5D6D7E",
    "error": "#8E44AD",
    "updating": "#8FB8E8",
}
LANG_QUICK = ("en", "ru", "uk", "de", "fr", "es", "it", "pl", "zh", "ja", "ko", "ar")
_ASSET_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "assets", "icon.png")


def _draw_icon(color_hex: str) -> Image.Image:
    """Fallback placeholder when the branded asset is missing."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgb = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    d.ellipse([2, 2, 62, 62], fill=rgb + (255,))
    d.rounded_rectangle([24, 14, 40, 36], radius=8, fill=(255, 255, 255, 255))
    d.arc([20, 26, 44, 48], start=20, end=160, fill=(255, 255, 255, 255), width=4)
    d.line([32, 48, 32, 54], fill=(255, 255, 255, 255), width=4)
    d.line([24, 54, 40, 54], fill=(255, 255, 255, 255), width=4)
    return img


def _load_icon() -> Image.Image:
    try:
        return Image.open(_ASSET_ICON).convert("RGBA").resize(
            (64, 64), Image.LANCZOS)
    except Exception:  # noqa: BLE001 - the tray must survive a failing backend
        return _draw_icon(STATE_COLORS["idle"])


class Tray:
    """Menu: pause, input device, model, recognition language, logs, exit."""

    def __init__(self, service, log: logging.Logger):
        self.service = service
        self.log = log
        self.icon = pystray.Icon(
            "VibeForma",
            icon=_load_icon(),
            title=service.tr("tray.starting"),
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda item: service.tr("tray.settings"), self._on_settings),
                pystray.MenuItem(
                    lambda item: service.tr("tray.show_panel"),
                    lambda *a: service.request_panel()),
                pystray.MenuItem(
                    lambda item: service.tr("tray.onboarding"),
                    lambda *a: service.open_onboarding()),
                pystray.MenuItem(
                    lambda item: service.tr("tray.diagnostics"),
                    lambda *a: service.open_doctor()),
                pystray.MenuItem(
                    lambda item: service.tr(
                        "tray.release_notes",
                        ver=(service.pending_update or {}).get("tag", "")),
                    lambda *a: service.open_release_notes(),
                    visible=lambda item: bool(
                        updater.release_notes_url(service.pending_update))),
                pystray.MenuItem(
                    lambda item: service.tr(
                        "tray.update_to", ver=service.pending_update["tag"]),
                    lambda *a: service.apply_pending_update(),
                    visible=lambda item: service.pending_update is not None),
                pystray.MenuItem(
                    lambda item: service.tr("tray.update_later"),
                    lambda *a: service.dismiss_pending_update(),
                    visible=lambda item: service.pending_update is not None),
                pystray.MenuItem(
                    lambda item: (service.tr("tray.resume")
                                  if service.state == "paused"
                                  else service.tr("tray.pause")),
                    self._on_pause),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda item: service.tr("tray.input_device"),
                    self._devices_submenu),
                pystray.MenuItem(
                    lambda item: service.tr("tray.model"), self._models_submenu),
                pystray.MenuItem(
                    lambda item: service.tr("tray.lang"), self._lang_submenu),
                pystray.MenuItem(
                    lambda item: service.tr("tray.history"),
                    lambda *a: service.open_settings("history")),
                pystray.MenuItem(
                    lambda item: service.tr("tray.undo"),
                    lambda *a: service.undo_last()),
                pystray.MenuItem(
                    lambda item: service.tr("tray.logs"), self._on_open_logs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda item: service.tr("tray.exit"), self._on_exit),
            ),
        )

    # -- menu --------------------------------------------------------------
    def _lang_label(self, code):
        mark = "•  " if self.service.cfg.get("language") == code else ""
        return mark + LANG_NAMES.get(code, code)

    def _lang_submenu(self, icon, item):
        return [pystray.MenuItem(
            self._lang_label(c), lambda *a, c=c: self.service.set_language(c))
            for c in LANG_QUICK]

    def _devices_submenu(self, icon, item):
        items = []
        try:
            names = self.service.input_device_names()
        except Exception:  # noqa: BLE001 - the tray must survive a failing backend
            names = []
        for name in names:
            mark = "•  " if self.service.is_current_device(name) else ""
            label = mark + (name if len(name) <= 48 else name[:45] + "…")
            items.append(pystray.MenuItem(
                label, lambda *a, n=name: self.service.set_device_by_name(n)))
        return items or [pystray.MenuItem(
            self.service.tr("tray.no_devices"), None, enabled=False)]

    def _models_submenu(self, icon, item):
        items = []
        for m in MODELS:
            mark = "•  " if self.service.current_model() == m else ""
            items.append(pystray.MenuItem(
                mark + m, lambda *a, n=m: self.service.set_model(n)))
        return items

    def _on_settings(self, icon, item):
        self.service.open_settings()

    def _on_pause(self, icon, item):
        self.service.toggle_pause()

    def _on_open_logs(self, icon, item):
        with contextlib.suppress(Exception):
            os.startfile(  # noqa: S606 - the user asked for the log folder
                os.path.dirname(self.service.log_path()))

    def _on_exit(self, icon, item):
        self.service.shutdown()

    # -- state -------------------------------------------------------------
    def set_state(self, state: str):
        try:
            label = self.service.tr(f"states.{state}",
                                    hotkey=self.service.cfg.get("hotkey", ""))
            if label == f"states.{state}":  # unknown key -> raw state
                label = state
            self.icon.title = "VibeForma: " + label
        except Exception:  # noqa: BLE001 - the tray must survive a failing backend
            self.log.debug("Tray: failed to update state %s", state)

    def notify(self, msg: str, title: str = "VibeForma"):
        try:
            self.icon.notify(msg, title)
        except Exception:  # noqa: BLE001 - the tray must survive a failing backend
            self.log.debug("Tray: notification failed: %s", msg)

    def run(self):
        self.icon.run()

    def run_detached(self):
        self.icon.run_detached()

    def stop(self):
        with contextlib.suppress(Exception):
            self.icon.stop()
