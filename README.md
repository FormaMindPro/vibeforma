# VibeForma

**from voice vibe to sharp form.**

Local voice dictation for Windows: press a hotkey → speak → the text lands
where your cursor is (editor, chat, browser — anywhere). Recognition runs on
your own machine via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
and understands **~99 languages** — the same downloaded model serves them all.
Nothing leaves your computer; the network is used exactly once, to download
the model.

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
- Layout-independent pasting (Shift+Insert) with the old clipboard restored
  after 1 s
- Settings window — silence pause, streaming interval, max recording length,
  panel transparency, autogrow, sounds, hotkey, input device, autostart — all
  applied live, no restart
- Tray menu: pause, input device, model switch without restart, recognition
  language, logs folder
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
automatically. With options:

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

Installer flags:

| Flag | Meaning |
| --- | --- |
| `--model <name>` | model to install (default `large-v3-turbo`) |
| `--target <folder>` | service folder (default `%LOCALAPPDATA%\VibeForma`) |
| `--dry-run` | print the plan, execute nothing |
| `--cpu` | force CPU (int8) even with CUDA present |
| `--no-run` | don't launch the service at the end |

## Models

| Model | Menu name | Size | Speed | Quality |
| --- | --- | --- | --- | --- |
| large-v3 | «large-v3» | 3.1 GB | baseline | best punctuation |
| large-v3-turbo | «large-v3-turbo» | 1.6 GB | fast | near large-v3 |
| medium | «medium» | 1.5 GB | ×4 vs large-v3 | medium |
| small | «small» | 0.5 GB | ×10 vs large-v3 | lower |

Switch models in the tray, no restart. Models live in
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

- Recording and recognition are local processes. The single network contact
  is the first model download; you can pre-place the model into `models/`
  and the install stays fully offline.
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
- **Text lands in the wrong window** — the service restores focus to the
  window that was active at the hotkey press; don't switch windows while
  dictating.
- **Russian layout and Ctrl+V** — pasting defaults to Shift+Insert, which is
  layout-independent; switchable in settings.

## Reviewing the code with an AI assistant

A practice worth keeping: before pushing, run the code through any AI
assistant with this prompt:

```text
You are reviewing a small Python app for Windows (voice dictation,
faster-whisper). Read app/*.py, install.py and find:
1) thread and race bugs (Tk only in its own thread, dicts shared across threads);
2) resource leaks (model, audio stream, handles);
3) any place the code could touch the network without the user asking
   (privacy: the network is allowed only for the first model download);
4) Windows platform traps (layouts, window focus, non-ASCII paths);
5) simplifications that would break a fresh clone's installation.
Answer as a list: file:line — problem — how to fix. Do not rewrite the code.
```

## License

MIT — see [LICENSE](LICENSE).
