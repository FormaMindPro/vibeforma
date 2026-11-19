"""Tray icon and menu (pystray + Pillow)."""
import logging
import os

import pystray
from PIL import Image, ImageDraw

STATE_COLORS = {
    "loading": "#F39C12",
    "idle": "#2E86DE",
    "recording": "#E74C3C",
    "processing": "#F39C12",
    "paused": "#5D6D7E",
    "error": "#8E44AD",
}
STATE_TITLES = {
    "loading": "Загрузка модели…",
    "idle": "Готов (Ctrl+Alt+Space)",
    "recording": "Идёт запись…",
    "processing": "Распознавание…",
    "paused": "Пауза",
    "error": "Ошибка модели",
}
MODELS = ("large-v3", "large-v3-turbo", "medium", "small")


def _draw_icon(color_hex: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgb = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    d.ellipse([2, 2, 62, 62], fill=rgb + (255,))
    d.rounded_rectangle([24, 14, 40, 36], radius=8, fill=(255, 255, 255, 255))
    d.arc([20, 26, 44, 48], start=20, end=160, fill=(255, 255, 255, 255), width=4)
    d.line([32, 48, 32, 54], fill=(255, 255, 255, 255), width=4)
    d.line([24, 54, 40, 54], fill=(255, 255, 255, 255), width=4)
    return img


class Tray:
    """Menu: pause, input device, model, logs folder, exit."""

    def __init__(self, service, log: logging.Logger):
        self.service = service
        self.log = log
        self.icon = pystray.Icon(
            "WhisperDictation",
            icon=_draw_icon(STATE_COLORS["loading"]),
            title="Whisper Диктовка: запуск…",
            menu=pystray.Menu(
                pystray.MenuItem("Настройки…", self._on_settings),
                pystray.MenuItem(
                    lambda item: self._pause_text(), self._on_pause),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Устройство ввода", self._devices_submenu),
                pystray.MenuItem("Модель", self._models_submenu),
                pystray.MenuItem("Папка логов", self._on_open_logs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", self._on_exit),
            ),
        )

    # -- menu --------------------------------------------------------------
    def _pause_text(self):
        return "Продолжить" if self.service.state == "paused" else "Пауза"

    def _devices_submenu(self, icon, item):  # noqa: ARG002
        items = []
        try:
            names = self.service.input_device_names()
        except Exception:
            names = []
        for name in names:
            mark = "•  " if self.service.is_current_device(name) else ""
            label = mark + (name if len(name) <= 48 else name[:45] + "…")
            items.append(pystray.MenuItem(
                label, lambda *a, n=name: self.service.set_device_by_name(n)))
        return items or [pystray.MenuItem("нет устройств ввода", None, enabled=False)]

    def _models_submenu(self, icon, item):  # noqa: ARG002
        items = []
        for m in MODELS:
            mark = "•  " if self.service.current_model() == m else ""
            items.append(pystray.MenuItem(
                mark + m, lambda *a, n=m: self.service.set_model(n)))
        return items

    def _on_settings(self, icon, item):  # noqa: ARG002
        self.service.open_settings()

    def _on_pause(self, icon, item):  # noqa: ARG002
        self.service.toggle_pause()

    def _on_open_logs(self, icon, item):  # noqa: ARG002
        try:
            os.startfile(os.path.dirname(self.service.log_path()))
        except Exception:
            pass

    def _on_exit(self, icon, item):  # noqa: ARG002
        self.service.shutdown()

    # -- state -------------------------------------------------------------
    def set_state(self, state: str):
        try:
            self.icon.icon = _draw_icon(STATE_COLORS.get(state, STATE_COLORS["idle"]))
            self.icon.title = "Whisper Диктовка: " + STATE_TITLES.get(state, state)
        except Exception:
            self.log.debug("Трей: не удалось обновить состояние %s", state)

    def notify(self, msg: str, title: str = "Whisper Диктовка"):
        try:
            self.icon.notify(msg, title)
        except Exception:
            self.log.debug("Трей: уведомление не прошло: %s", msg)

    def run(self):
        self.icon.run()

    def run_detached(self):
        self.icon.run_detached()

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass
