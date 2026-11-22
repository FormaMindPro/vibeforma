# VibeForma

**from voice vibe to sharp form.**

Local voice dictation for Windows: press a hotkey → speak → the text lands
where your cursor is (editor, chat, browser — anywhere). Recognition runs on
your own machine via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
and understands **~99 languages** — the same downloaded model serves them all.
Nothing leaves your computer; the network is used exactly once, to download
the model.

Russian version: [README.ru.md](README.ru.md).

[![CI](https://github.com/FormaMindPro/vibeforma/actions/workflows/ci.yml/badge.svg)](https://github.com/FormaMindPro/vibeforma/actions/workflows/ci.yml)

## Quick start

Windows 10/11 x64, Python 3.10+ on `PATH`. One command in PowerShell:

```powershell
irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex
```

It finds Python, downloads the latest release, installs everything into
`%LOCALAPPDATA%\VibeForma` (its own `.venv`, the dependencies, the speech
model, shortcuts and a Settings > Apps entry) and starts the service. Press
`Ctrl+Alt+Space` in any app and speak.

The installer asks where to install: press Enter for
`%LOCALAPPDATA%\VibeForma`, or type another folder such as `D:\VibeForma`.

To launch it later: **Start menu → VibeForma**, the desktop shortcut, or
`%LOCALAPPDATA%\VibeForma\run.cmd`. The service icon sits in the tray.

Options go after the script block:

```powershell
iex "& { $(irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1) } -Model large-v3 -NoRun"
```

- `-Model <name>` — install this model instead of showing the choice dialog
- `-Target <folder>` — install folder without the question (default:
  `%LOCALAPPDATA%\VibeForma`)
- `-Cpu` — CPU only (int8), even when CUDA is present
- `-DryRun` — print the plan and change nothing
- `-NoRun` — do not launch the service at the end

From `cmd.exe`:
`powershell -c "irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex"`

The bootstrap installs released builds only: when no release zip can be
downloaded it stops with the reason and never falls back to the main branch.
Other ways to install (the setup without Python, a release zip, a git
clone), every flag and uninstall — [Install](#install).

## Updating

The service checks GitHub releases in the background (every 2 h by default;
the switch and the interval live in Settings). When a newer release is
found, a blue **Update** button appears on the monitoring panel and in the
tray. One click runs the same installer as the quick start, keeps the model,
dictionary, history and `config.json`, and restarts the service.

To update by hand, close VibeForma from the tray and run the quick-start
command again. If the app lives outside the default folder, add `-Target`:

```powershell
iex "& { $(irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1) } -Target "D:\VibeForma"
```

The update is the same GitHub release zip as the first installation; nothing
else is downloaded and no extra step is required.

## Features

- Hotkey `Ctrl+Alt+Space` (remappable): start speaking, the phrase closes on a
  pause of silence or on the second keypress
- Recognition language: **pinned exactly** — the model recognizes exactly the
  language you set, no per-phrase guessing; switch in the tray menu or
  settings (26 codes)
- Interface: English and Russian out of the box; add a JSON file to
  `app/locales/` to translate it into anything else
- Monitoring panel: mic level, VAD indicator, recognized text streaming
  near-real-time while you speak, an Update button when a release is
  pending, a microphone button, and ‹ › arrows to flip through saved
  dictations (with history on): press the microphone and the recognized
  text waits in the panel with **Edit**, **Copy**, **Insert at the cursor**
  and **Cancel** underneath — no separate popup, nothing is pasted into a
  window you did not pick. While the model is still recognizing, the panel
  dims and a fast percentage with a thin running line crosses it
- Layout-independent pasting (Shift+Insert) that never steals the focus: if
  the window changed during dictation, it offers "paste here / copy /
  cancel" instead of pasting into a stranger. The old clipboard is restored
  after 1 s, but only if you have not copied something new meanwhile; on
  Windows 10+ the dictated text is kept out of the clipboard history and
  cloud sync
- Settings window, organized in tabs (Recording / Model & language /
  Dictionary / History / Appearance / System): silence pause, streaming
  interval, max recording length, panel transparency, autogrow, sounds,
  hotkey, input device, autostart — all applied live, no restart
- **Dictionary**: terms are added to the recognition prompt, replacements are
  applied to the recognized text (case-insensitive, the longest term wins);
  import/export as JSON and a preview that shows exactly what a sample text
  becomes
- **History (off by default)**: the last 100 dictations stay on this computer
  in `history.jsonl`, shown as a full-width table with when / chars /
  application / text columns and a phrase search. Select rows to copy, paste
  again or delete (several at once), use the actions above the table; the
  history switches stay below it. Turning it off deletes the file; the
  dictated text never goes to the log. "Undo last insert" sends Ctrl+Z only
  to the same window within 30 s, otherwise it warns instead; "Restore
  clipboard" puts the pre-dictation clipboard back
- Models: pick one during install (a choice dialog with sizes and blurbs),
  then switch anytime in settings or the tray menu — the new model downloads
  first, the service reloads without a restart, and the previous model is
  kept on disk or deleted, at your choice; a failed download rolls back
- Tray menu: pause, input device, model switch without restart, recognition
  language, history and "undo last insert", diagnostics, logs folder
- Auto-update: the service checks GitHub releases in the background (default
  every 2 h; the interval and the on/off switch live in settings, plus a
  "Check now" button). A newer release is offered, not forced: the panel and
  the tray show an Update button and Later, and only the explicit "install
  automatically" opt-in skips the question. The notification carries a short
  excerpt of the release notes, and the tray adds "What's new in …" with a
  link to the release page. One click runs the same GitHub installation
  command as [Updating](#updating).
- Autostart with Windows, desktop shortcut, single-instance lock, rotating logs

## Install

Windows 10/11, Python 3.10+ (3.12+ recommended). With an NVIDIA GPU the
recognition is fast (CUDA float16); without one the installer falls back to
CPU (int8) and preselects the `small` model.

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
`config.json` → creates shortcuts (desktop, Start menu and startup, branded
icon) → records the product in Settings > Apps → starts the service.

Base dependencies only come from `app/requirements.txt`; the CUDA wheels
(`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) are installed from
`app/requirements-cuda.txt` only when CUDA is detected — a CPU install never
downloads them. On CPU the dialog preselects the `small` model and
`config.json` gets `device: "cpu"`, `compute_type: "int8"`; an existing
config that still says `cuda` on a machine without CUDA is corrected (a
console question on an interactive run, silence plus a log line when
unattended). The recognition language preselects your Windows locale (`ru`
on Russian Windows), `en` when the locale has no matching code; change it in
the dialog or with `--language`.

If the old `%LOCALAPPDATA%\WhisperDictation` folder is found, its
`config.json` (with `hotwords`) and downloaded `models` are copied into the
new install, and the old desktop/startup/Start menu shortcuts are removed —
so two copies never autostart. The old folder itself is copied, not deleted:
remove it after the new version starts cleanly.

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
| `--language <code>` | recognition language; default: your Windows locale, `en` when it has no matching code |
| `--target <folder>` | service folder (default `%LOCALAPPDATA%\VibeForma`) |
| `--dry-run` | print the plan, execute nothing |
| `--cpu` | force CPU (int8) even with CUDA present |
| `--no-run` | don't launch the service at the end |
| `--log <file>` | install log path (default `%LOCALAPPDATA%\VibeForma\logs\install.log`) |

## First run

The first launch opens a three-step wizard. Step 1 picks the interface and
recognition language. Step 2 picks the microphone: the level moves as it
hears you, and "Test microphone" makes a short recording and shows the
recognized text — nothing is inserted. Step 3 records the hotkey by pressing
it: modifiers alone are refused, and a combination Windows already uses is
only a warning. "Skip" writes the safe defaults. Reopen the wizard from the
tray ("Setup wizard…") or from Settings > System; `onboarding_done` in
`config.json` remembers that it ran.

## Uninstall

VibeForma is listed in **Settings > Apps** (per user, no admin rights).
Uninstall it from there, or run `uninstall.cmd` in the service folder. The
uninstaller removes the shortcuts, the startup entry, the Settings > Apps
record and the service files. Models and `config.json` (with your `hotwords`)
stay unless you pass `--remove-data`, so a reinstall can start offline.
`--keep-data` skips the question and always keeps them.

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
is nowhere on disk. For an offline install, put a model folder into
`models\<name>` (or the shared Hugging Face cache) beforehand: the installer
and the service then need no network at all.

Measured on the 12.5 s test phrase (CUDA float16, best of 3):

| Model | Recognition | Note |
| --- | --- | --- |
| large-v3 | 1.53 s | richer punctuation |
| large-v3-turbo | 0.64 s (~2.4× faster) | all words correct, sparser punctuation |

On short phrases the real turbo speedup is below the vendor's 4–8× (a larger
share of non-scaling stages); the gap widens on longer audio.

## Languages

- **Recognition**: pinned exactly — the model recognizes exactly the chosen
  language (default `en`; the installer preselects your Windows locale);
  switch in the tray or settings. No auto-detection:
  what you set is what is recognized. A per-language prompt preset improves
  punctuation for the major languages; personal terms belong to
  `config.json → "hotwords"`.
- **Menu naming**: every language is written in its own script —
  English, Russian, Ukrainian, German, Japanese…
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
  accounts, no tokens. The log holds no dictated text and no window titles;
  `log_window_titles: true` in `config.json` adds the title of the paste
  target if you need it for a diagnosis.
- On Windows 10+ the dictated text is marked as excluded from the clipboard
  history and cloud sync. The monitoring panel has a privacy mode (the eye
  button): the text is hidden, the mic level stays, and the status line
  disappears 30 s after it was set.

## What stays on disk

- `config.json` — your settings and `hotwords` (the terms you typed there);
- `models\` — downloaded Whisper models;
- `logs\` — the service log (five rotated files, 1 MB each), no dictated
  text and no window titles by default;
- `_http_in_*.wav` — only while the local HTTP API handles a request; deleted
  in any case, success or failure;
- `install.log` — the installer's own log;
- no audio recordings are kept.

## Local HTTP API (off by default)

Turn it on in Settings → **Voice for external forms** → **Allow the local API
for forms**. The key is minted and saved automatically. The API listens on
`127.0.0.1` only and answers `GET /health` and `POST /transcribe` with the
header `X-VibeForma-Token: <secret>`. `/health` returns `ok`, `app`
(`vibeforma`), `api`, the actual `port`, `version`, `model`, `language` and
`state`; `/transcribe` takes `Content-Type: audio/wav`, a WAV body up to
10 MB, one request per second, only while the service is idle, and answers
`{"text": "..."}`; the uploaded audio is written to a temporary file and
deleted in any case.

The service publishes `%LOCALAPPDATA%\VibeForma\location.json` with
`schema`, `app`, `version`, `config`, `api` (`on`, `off` or `error`) and the
actual `port`. If the configured port (`8787` by default) is busy, the
service binds a free loopback port and writes that port here, so a form reads
the endpoint from this file instead of hardcoding a port. The file carries no
secret: the token stays in `config.json`.

## Command line and external hook

The installed copy answers three flags from the command line:
`VibeForma.exe --toggle` starts recording or stops it when it is running,
`--cancel` drops the current recording or the panel draft, and `--status`
prints the running instance's `status.json` (`state`, `version`, `pid`, `ts`)
as JSON. The flags talk to the instance that is already running: with no
instance, or when the status is older than 60 s, the exit code is 1; on
success it is 0.

The hook is opt-in and off while `"hook_command"` is empty. Set it in
settings → System (or in `config.json` next to `"hook_timeout_s"`, default 5):
the recognized text is written to the command's stdin as UTF-8. The command
runs without a shell, so `&`, `|` and `>` in the text are plain characters;
the timeout is kept inside 1–30 s. A failure or a timeout is logged as a
status only — the text never reaches the log — and never affects insertion.
The hook runs in parallel, after the text is inserted or shown as a panel
draft.

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
  doctor.py            support report: --doctor, tray/settings diagnostics
  config.py            config.json, defaults, device lookup, CUDA DLL bootstrap
  i18n.py              UI strings loader (locales/*.json, en fallback)
  locales/             en.json, ru.json — drop a file to add a language
  assets/              icon.png, icon.ico
  requirements.txt     base dependencies (no CUDA wheels)
  requirements-cuda.txt  base + CUDA wheels, used only on CUDA machines
  run.cmd              launcher: pythonw main.py | --selftest | --doctor
  uninstall.cmd        uninstaller entry point
  uninstall.py         uninstaller (stdlib): shortcuts, Settings > Apps, files
install.cmd            one-click installer
install.py             installer (python, stdlib)
```

## Troubleshooting

Start with the built-in report: `run.cmd --doctor` (or tray → **Diagnostics…**)
lists Python, dependencies, model, device, microphone, hotkey, update
settings, disk space and privacy switches with OK / WARN / FAIL and exits 0
or 1. The report has no dictated text and no window titles; the window has a
**Copy report** button. The full guide, including the HTTP API port, disk
space and SmartScreen: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

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

`tools/ruff_gate.py` runs the same selection as CI and fails on any finding:
the 164 findings recorded in 0.1.9 were cleaned up, so `ruff` is blocking.

### Language of the repository

The repository is English-only outside localization resources: code,
comments, logs, the changelog and commit messages are written in English.
Cyrillic is allowed only in `app/locales/*.json`, the installer's Russian
texts, the ru/uk ASR prompts and `README.ru.md`. `tests/test_english_only.py`
enforces the rule, so a stray Cyrillic character fails the suite.

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
