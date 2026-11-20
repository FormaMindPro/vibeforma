# PyInstaller one-folder build: program files only, models are downloaded
# on the first run and live in the data folder (LOCALAPPDATA\VibeForma).
# Build: python tools/build_installer.py   (or pyinstaller packaging/vibeforma.spec)
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
APP = os.path.join(ROOT, "app")

datas = [
    (os.path.join(APP, "assets"), "assets"),
    (os.path.join(APP, "locales"), "locales"),
    (os.path.join(ROOT, "test_ru.wav"), "."),
]
datas += collect_data_files("faster_whisper")

a = Analysis(
    [os.path.join(SPECPATH, "entrypoint.py")],
    pathex=[APP],
    binaries=[],
    datas=datas,
    hiddenimports=["pystray._win32"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "ruff", "mypy", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VibeForma",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(APP, "assets", "icon.ico"),
    version=os.environ.get("VIBEFORMA_VERSION_FILE") or None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VibeForma",
    contents_directory="_internal",
)
