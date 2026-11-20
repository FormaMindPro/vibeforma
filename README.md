# VibeForma

**from voice vibe to sharp form.**

Local voice dictation for Windows: press a hotkey → speak → the text lands
where your cursor is (editor, chat, browser — anywhere). Recognition runs on
your own machine via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
and understands **~99 languages** — the same downloaded model serves them all.
Nothing leaves your computer; the network is used exactly once, to download
the model.

[![CI](https://github.com/FormaMindPro/vibeforma/actions/workflows/ci.yml/badge.svg)](https://github.com/FormaMindPro/vibeforma/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Features

- Hotkey `Ctrl+Alt+Space` (remappable): start speaking, the phrase closes on a
  pause of silence or on the second keypress
- Recognition language: **pinned exactly** — the model recognizes exactly the
  language you set, no per-phrase guessing; switch in the tray menu or
  settings (26 codes)
- Interface: English and Russian out of the box; add a JSON file to
  `app/locales/` to translate it into anything else
- Monitoring panel: mic level, VAD indicator, recognized text streaming
  near-real-time while you speak
- Layout-independent pasting (Shift+Insert) that never steals the focus: if
  the window changed during dictation, it offers "paste here / copy /
  cancel" instead of pasting into a stranger. The old clipboard is restored
  after 1 s, but only if you have not copied something new meanwhile; on
  Windows 10+ the dictated text is kept out of the clipboard history and
  cloud sync
- Settings window, organized in tabs (Recording / Model & language /
  Appearance / System): silence pause, streaming interval, max recording
  length, panel transparency, autogrow, sounds, hotkey, input device,
  autostart — all applied live, no restart
- Models: pick one during install (a choice dialog with sizes and blurbs),
  then switch anytime in settings or the tray menu — the new model downloads
  first, the service reloads without a restart, and the previous model is
  kept on disk or deleted, at your choice; a failed download rolls back
- Tray menu: pause, input device, model switch without restart, recognition
  language, logs folder
- Auto-update: the service checks GitHub releases in the background (default
  every 2 h; the interval and the on/off switch live in settings, plus a
  "Check now" button). A newer release is offered, not forced: the tray shows
  Update / Later, and only the explicit "install automatically" opt-in skips
  the question. The release manifest is signed (minisign/Ed25519); the updater
  verifies the signature, the zip hash and the file list before running the
  installer, keeps a backup and restores the previous version on failure.
- Autostart with Windows, desktop shortcut, single-instance lock, rotating logs

## Install

Windows 10/11, Python 3.10+ (3.12+ recommended). With an NVIDIA GPU the
recognition is fast (CUDA float16); without one the installer falls back to
CPU (int8) — use the `small` model there.

**One command (PowerShell, Windows 10+):**

```powershell
irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex
```

The bootstrap downloads the latest release, unpacks it and runs the
installer — everything else (venv, dependencies, model, shortcuts) happens
automatically. It installs a released build only: when no release zip is
available it stops and explains why instead of falling back to the main
branch. Without `-Model` the model is chosen in a small dialog during
install. With options:

```powershell
iex "& { $(irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1) } -Model large-v3 -NoRun"
```

(from `cmd.exe`: `powershell -c "irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex"`)

**Option A — release zip:** grab `VibeForma-<version>.zip` from
[Releases](../../releases), unzip, double-click `install.cmd`.

**Option B — git:**

```bat
git clone https://github.com/FormaMindPro/vibeforma.git
cd vibeforma
install.cmd
```

The installer: copies `app/` to `%LOCALAPPDATA%\VibeForma` → creates a `.venv`
→ installs dependencies → downloads the model (the only network step; skipped
if the model is already in the shared Hugging Face cache) → writes
`config.json` → creates shortcuts (desktop + startup, branded icon) → starts
the service.

Before writing anything it checks Windows 10 build 1809+, x64/ARM64,
Python 3.10–3.14, free space (model + 4 GB) and the target — an empty
folder, an existing VibeForma install, or a folder marked by
`.vibeforma-install.json` from a previous run.
System folders, Program Files, drive roots and the profile root are refused.
The first failed step stops the chain: no shortcuts, no config, no launch.
A failed first install removes its folder; an existing install is restored
from a backup. `install.log` (default `%LOCALAPPDATA%\VibeForma\logs`,
override with `--log`) records every step, command and error tail, and the
console prints its path. Re-running continues from the failed step — the
venv, model and config already in place are reused.

Installer flags:

| Flag | Meaning |
| --- | --- |
| `--model <name>` | model to install; without it a choice dialog shows (on an existing install the configured model is kept) |
| `--target <folder>` | service folder (default `%LOCALAPPDATA%\VibeForma`) |
| `--dry-run` | print the plan, execute nothing |
| `--cpu` | force CPU (int8) even with CUDA present |
| `--no-run` | don't launch the service at the end |
| `--log <file>` | install log path (default `%LOCALAPPDATA%\VibeForma\logs\install.log`) |

## Models

| Model | Menu name | Size | Speed | Quality |
| --- | --- | --- | --- | --- |
| large-v3 | «large-v3» | 3.1 GB | baseline | best punctuation |
| large-v3-turbo | «large-v3-turbo» | 1.6 GB | fast | near large-v3 |
| medium | «medium» | 1.5 GB | ×4 vs large-v3 | medium |
| small | «small» | 0.5 GB | ×10 vs large-v3 | lower |

Pick the model during install (the dialog shows size and description); switch
later in settings (the *Model & language* tab) or the tray menu. The switch
downloads the new model first, reloads the service without a restart and then
— at your choice — keeps the previous model on disk for fast switching back
or deletes it (a model in the shared Hugging Face cache is left there; a
failed download leaves the old model untouched). Models live in
`%LOCALAPPDATA%\VibeForma\models\<name>` or in the shared Hugging Face cache;
the service loads offline-first and only reaches the network when the model
is nowhere on disk.

Measured on the 12.5 s test phrase (CUDA float16, best of 3):

| Model | Recognition | Note |
| --- | --- | --- |
| large-v3 | 1.53 s | richer punctuation |
| large-v3-turbo | 0.64 s (~2.4× faster) | all words correct, sparser punctuation |

On short phrases the real turbo speedup is below the vendor's 4–8× (a larger
share of non-scaling stages); the gap widens on longer audio.

## Languages

- **Recognition**: pinned exactly — the model recognizes exactly the chosen
  language (default `en`); switch in the tray or settings. No auto-detection:
  what you set is what is recognized. A per-language prompt preset improves
  punctuation for the major languages; personal terms belong to
  `config.json → "hotwords"`.
- **Menu naming**: every language is written in its own language —
  English, Русский, Українська, Deutsch, 日本語…
- **Interface**: English and Russian ship in `app/locales/*.json`. To add
  yours — copy `en.json`, translate the values, save as `app/locales/<code>.json`;
  the language then appears in settings automatically.

## Usage

1. Start the service (desktop shortcut or autostart) — a tray icon appears,
   the monitoring panel opens.
2. Check the panel sees your microphone (the level bar moves when you speak;
   otherwise pick the device in the tray).
3. `Ctrl+Alt+Space` → speak → pause → the text is inserted into the field
   that had focus when you pressed the hotkey.
4. `✕` hides the panel (the tray icon stays), `⚙` opens settings.

## Privacy

- Recording and recognition are local processes. Network contacts: the first
  model download at install; and if auto-update is on (the default), a small
  check of `api.github.com` every 2 h — the interval is configurable and the
  check can be switched off in settings. The release zip downloads only when
  a newer version exists. Nothing else leaves the machine.
- Logs live in the local `logs\` folder with rotation. No telemetry, no
  accounts, no tokens.

## Repository layout

```
app/
  main.py              service: states, hotkey, record/recognize threads, selftest
  audio.py             16 kHz capture, level, Silero VAD, partial transcribes
  asr.py               faster-whisper: offline-first loading, transcription, model swap
  insert.py            text insertion (Shift+Insert/Ctrl+V), clipboard restore, focus
  hotkey.py            global hotkey (pynput)
  tray.py              tray icon and menu (pystray)
  hud.py               monitoring panel (tkinter, own thread)
  settings_window.py   settings dialog (tkinter)
  config.py            config.json, defaults, device lookup, CUDA DLL bootstrap
  i18n.py              UI strings loader (locales/*.json, en fallback)
  locales/             en.json, ru.json — drop a file to add a language
  assets/              icon.png, icon.ico
  requirements.txt
  run.cmd              launcher: pythonw main.py | --selftest
install.cmd            one-click installer
install.py             installer (python, stdlib)
```

## Troubleshooting

- **Python not found** — install from python.org (tick "Add to PATH").
- **No CUDA** — the installer picks CPU int8 automatically; prefer
  `--model small` on CPU.
- **The mic hears nothing** — choose the input device in the tray; the panel
  shows a silence warning when the level stays near zero while recording.
- **The text was not pasted after dictation** — the active window changed
  while you spoke. VibeForma asks where to put the text; "Copy" leaves it in
  the clipboard, and the tray message says so. Set "ask where to paste" off
  to copy silently in that case.
- **Russian layout and Ctrl+V** — pasting defaults to Shift+Insert, which is
  layout-independent; switchable in settings.

## Development

The test suite runs without the model, a GPU or the network. Windows CI runs
the full suite; Linux CI runs the platform-independent tests (`-m "not windows"`).

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python tools\ruff_gate.py
```

`tools/ruff_gate.py` compares `ruff` output with `tools/ruff-baseline.json`:
the 164 findings recorded in 0.1.9 are a ceiling, and any new finding fails
the gate. Fixing the recorded ones is a separate cleanup task.

## Reviewing the code with an AI assistant

A practice worth keeping: before pushing, run the code through any AI
assistant with this prompt:

```text
You are reviewing a small Python app for Windows (voice dictation,
faster-whisper). Read app/*.py, install.py and find:
1) thread and race bugs (Tk only in its own thread, dicts shared across threads);
2) resource leaks (model, audio stream, handles);
3) any place the code could touch the network without the user asking
   (privacy: the network is allowed only for the first model download and
   the release check when auto-update is on);
4) Windows platform traps (layouts, window focus, non-ASCII paths);
5) simplifications that would break a fresh clone's installation.
Answer as a list: file:line — problem — how to fix. Do not rewrite the code.
```

## License

MIT — see [LICENSE](LICENSE).
