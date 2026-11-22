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
`schema` (2), `app`, `version`, `launcher`, `python`, `entry`, `install_dir`,
`pipe`, `config`, `api` (`on`, `off` or `error`), `file_mode`, `formats`,
`languages`, `max_file_mb`, `max_file_s` and the actual `port`. If the
configured port (`8787` by default) is busy, the service binds a free
loopback port and writes that port here, so a form reads the endpoint from
this file instead of hardcoding a port. The file carries no secret: the pipe
is protected by its own DACL and the token stays in `config.json`.

The file mode adds three kinds of routes, all on `127.0.0.1` and all with the
same token:

- `GET /capabilities` - version, models, languages, formats, limits,
  `file_mode`, `word_timestamps`, `dictation_priority`, the pipe and the
  launcher;
- `POST /v1/audio/transcriptions` - OpenAI-compatible multipart
  (`file`, `model`, `language`, `response_format`, `prompt`, `temperature`);
  `response_format` is `json`, `text`, `srt`, `vtt` or `verbose_json`;
  synchronous up to `file_max_sync_s` (900 s), longer files get `413` with a
  pointer to `/jobs`;
- `POST /jobs`, `GET /jobs/<id>`, `DELETE /jobs/<id>` (cancel),
  `GET /jobs/<id>/result?format=srt|vtt|txt|json` - the queue for long files;
  the answer is `202` with `Location: /jobs/<id>`, and `409` until the result
  is ready.

`/health` keeps its old fields and adds `file_mode`, `jobs_running`,
`jobs_queued` and `dictation_priority`. The old `/transcribe` keeps its
contract: 10 MB, one request per second, idle only. A dictation always
outranks a file job: the job pauses between segments and resumes when the
service is idle.

## File transcription

Audio and video files go through the same engine as dictation: mp4, mkv,
mov, webm, mp3, wav, m4a. PyAV decodes them (the dependency faster-whisper
already brings), so no external ffmpeg is needed. The output is text with
timestamps in TXT, JSON, SRT or VTT.

Long file jobs (60 s and longer) are transcribed in batches: several 30-second
windows go through the model in one pass (`BatchedInferencePipeline`) on the
same loaded model, with the same language, VAD, prompt and anti-hallucination
settings. Short files and dictation stay on the sequential path. If the
batched path is unavailable or fails mid-file, the job falls back to the
sequential path without duplicates. The keys are `file_batched` (on by
default) and `file_batch_size` (8, kept within 1-16); the speed-up depends on
the device, and the measured figure on the reference machine is in
`CHANGELOG.md`.

From the tray: **Transcribe file...**, pick a file, watch the progress in the
panel, then use **Open result folder**. From the command line (the service
must be running):

```
run.cmd --transcribe "D:\video\talk.mp4" --lang ru --format srt
run.cmd --transcribe "D:\video\talk.mp4" --format all --json
.venv\Scripts\python.exe main.py --transcribe talk.mp4 --out out\talk.srt
```

- `--lang` is the recognition language (the config value otherwise, English
  as the last resort);
- `--format` is `txt`, `json`, `srt`, `vtt` or `all` (default `srt`);
- `--out` writes to a named file or folder; without it the result lands
  beside the source with the same stem;
- `--force` allows overwriting an existing result;
- `--json` prints `{"ok": true, "job_id": ..., "out": [...], "segments": N,
  "duration_s": ...}`; progress goes to stderr.

Exit codes: `0` success, `1` the service is not running or the job failed,
`2` bad arguments, `3` cancelled. While the service is still loading its
first model the CLI answers `VibeForma: the model is loading` and exits 1;
`VibeForma is not running` appears only when no channel listens. The request
travels over the current user's named pipe (`\\.\pipe\VibeForma-<user>`), so the CLI never opens a
port and never waits for the HTTP API.

Limits: `file_max_mb` (4096), `file_max_s` (6 hours), `job_keep_s` (24 hours
for a finished job), `file_roots` (empty = every local fixed or removable
disk; network, UNC and device paths are refused). The input file is never
copied or changed. No network is used for recognition or delivery.
VibeForma produces SRT/VTT; burning subtitles into a video or translating
them is a downstream tool, not this engine.

## Batch folder transcription

A whole folder goes through the same engine and the same queue as one file,
one file at a time. From the tray choose **Transcribe folder…** and pick a
folder; from the command line (the service must be running):

```
run.cmd --transcribe-folder "D:\media" --format all --json
run.cmd --transcribe-folder "D:\media" --recursive --out D:\out
.venv\Scripts\python.exe main.py --transcribe-folder media --lang ru
```

- the default is the top level of the folder; `--recursive` adds subfolders;
- supported files: mp4, mkv, mov, webm, mp3, wav, m4a, m4b, ogg, flac; other
  files are not picked up and are not an error;
- without `--out` every result lands beside its source; with `--out` the
  folder tree is mirrored (`D:\media\sub\b.mkv` becomes
  `D:\out\sub\b.srt`);
- an existing output is skipped, not overwritten; `--force` overwrites it;
- two sources with the same stem (`a.mp4`, `a.mkv`) produce one target: the
  second file is recorded as `duplicate output target` and the batch goes on;
- one broken file is recorded and the rest continue; the batch ends with the
  counters and a non-zero code when anything failed;
- dictation outranks the batch: it starts at once, the batch pauses between
  segments and resumes on the same file;
- `--cancel-batch [<id>]` stops the current file and the queue;
- `--json` prints one summary object with `total/done/skipped/failed/
  cancelled/progress/files`; progress goes to stderr.

The HUD shows a bottom strip while a file or a batch runs: a moving bar, the
current file name, its percent, and the batch counter `Batch: N/M — P%`. The
states are visibly different (queued, transcribing, paused for dictation,
done, failed), the error of one file stays visible while the batch goes on,
and the result hides 8 seconds after the end. The strip does not take focus
and does not touch the dictation overlay or the hotkey.

Exit codes: `0` every file done or skipped, `1` a file failed or the service
is not running, `2` bad arguments or a refused folder, `3` cancelled, `4`
nothing to transcribe in the folder. Network, UNC and device paths are
refused; drag&drop is not a file entrance.

## For agents and other harnesses

Any harness - Forma Mind, Claude Code, Cursor, Codex, a PowerShell script or
a Python process - uses the same OS-level contract; VibeForma does not know
which one is calling.

1. Read `%LOCALAPPDATA%\VibeForma\location.json`: `launcher`, `python`,
   `entry` and `pipe`. No file means the service is not running.
2. Call the launcher, or the venv python with the entry file, with
   `--transcribe`:

```powershell
# PowerShell
& "$env:LOCALAPPDATA\VibeForma\run.cmd" --transcribe "D:\video\talk.mp4" --lang ru --format srt --json
# cmd.exe
"%LOCALAPPDATA%\VibeForma\run.cmd" --transcribe "D:\video\talk.mp4" --format srt
# Python
subprocess.run([launcher, "--transcribe", path, "--format", "srt", "--json"], check=True)
# Node.js
spawnSync(launcher, ["--transcribe", path, "--format", "srt", "--json"])
```

3. With the local API on, `POST /v1/audio/transcriptions` (short files) or
   `POST /jobs` (long files) do the same over HTTP; `GET /capabilities`
   lists formats and limits.
4. `run.cmd --help` prints the contract, so a harness needs no source file.

Python without a shell can always use the second form:
`.venv\Scripts\python.exe main.py --transcribe ...`.

## Command line and external hook

The installed copy is a source copy: `run.cmd`, `main.py` and `.venv`
beside them, with no packed `VibeForma.exe`. Without arguments `run.cmd`
starts the panel through `pythonw.exe main.py`; with arguments it runs
`python.exe main.py` with those arguments, so stdout, stderr and the exit
code reach the caller. Both forms are equivalent:

```
run.cmd --toggle
run.cmd --cancel
run.cmd --status
.venv\Scripts\python.exe main.py --status
```

`--toggle` starts recording or stops it when it is running, `--cancel` drops
the current recording or the panel draft, and `--status` prints the running
instance's `status.json` (`state`, `version`, `pid`, `ts`) as JSON. The flags
talk to the instance that is already running: with no instance, or when the
status is older than 60 s, the exit code is 1; on success it is 0.
`--help` prints every command, and `--transcribe` is described under
File transcription.

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
  asr.py + transcript.py  file mode: PyAV decode, segments, TXT/JSON/SRT/VTT
  file_jobs.py         file job queue: dictation priority, pause, cancel, progress
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
  run.cmd              launcher: no args -> pythonw main.py; args -> python.exe main.py
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
