"""Frozen entry point of the packed build.

User data goes beside the program in a source install; a packed build keeps
it in %LOCALAPPDATA%\\VibeForma, so an upgrade or a reinstall never touches
config.json, models, logs or history.

Build diagnostics: VibeForma.exe --bundle-check imports every runtime
dependency, checks the bundled data and writes the result to
%TEMP%\\vibeforma-bundle-check.txt (a windowed exe has no console), then
exits 0 on success. VibeForma.exe --doctor prints the support report and
also writes it to the temp file vibeforma-doctor.txt.
"""
import os
import sys
import tempfile

RESULT = os.path.join(tempfile.gettempdir(), "vibeforma-bundle-check.txt")


def _report(lines) -> None:
    with open(RESULT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def bundle_check() -> int:
    lines = []
    try:
        # Every import below is the check itself.
        import tkinter  # noqa: F401

        import config
        import ctranslate2  # noqa: F401
        import faster_whisper  # noqa: F401
        import hud  # noqa: F401
        import i18n
        import numpy  # noqa: F401
        import pyperclip  # noqa: F401
        import pystray  # noqa: F401
        import sounddevice  # noqa: F401
        import tray  # noqa: F401

        for folder in ("assets", "locales"):
            path = os.path.join(os.path.dirname(config.__file__), folder)
            if not os.path.isdir(path):
                raise RuntimeError(f"missing data folder: {folder}")
        for code in ("en", "ru"):
            if i18n.t({"locale": code}, "settings.title") == "settings.title":
                raise RuntimeError(f"locale {code} lost its strings")
        if not os.path.isfile(config.TEST_WAV):
            raise RuntimeError("test WAV is not bundled")
        lines.append("BUNDLE OK " + config.VERSION)
        return 0
    except Exception as e:  # noqa: BLE001 - the check reports any failure
        lines.append("BUNDLE FAIL: " + repr(e))
        return 1
    finally:
        _report(lines)


def main() -> None:
    if (getattr(sys, "frozen", False)
            and "VIBEFORMA_DATA_DIR" not in os.environ):
        base = os.path.join(os.environ.get("LOCALAPPDATA",
                                           os.path.expanduser("~")),
                            "VibeForma")
        os.environ["VIBEFORMA_DATA_DIR"] = base
        os.makedirs(base, exist_ok=True)
    if "--bundle-check" in sys.argv:
        sys.exit(bundle_check())
    if "--doctor" in sys.argv:
        import doctor
        sys.exit(doctor.main(os.path.join(tempfile.gettempdir(),
                                          "vibeforma-doctor.txt")))
    from vibeforma.app import main as run

    run()


if __name__ == "__main__":
    main()
