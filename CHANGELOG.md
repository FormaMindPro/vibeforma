# Changelog

All notable changes to VibeForma are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.1.9 — 2026-11-20

### Fixed
- **The monitoring panel keeps its size at any display scaling** — its
  pixel geometry (width, height, paddings, level bar) scales with the DPI
  factor like the rest of the UI (at 150% it had shrunk 1.5×).

### Added
- **A record button on the panel** (🎤 / ⏹ while recording): clicking it
  starts and stops dictation — the same state-guarded toggle as the hotkey.

### Fixed
- The status line shows the actual hotkey ("Готов — Ctrl+Alt+Space")
  instead of the raw "{hotkey}" placeholder — in the panel and the tray
  tooltip alike.

## 0.1.8 — 2026-11-20

### Fixed
- **Tab labels are readable now.** The clam theme ignores a static tab
  foreground, so the inactive tabs rendered near-invisible dark-on-dark.
  All tab text colors now go through the style state map (white on the
  selected tab, light gray on the rest).

## 0.1.7 — 2026-11-20

### Fixed
- **The relaunch after an update now really starts.** Even with the
  non-daemon spawner (0.1.6), `os.startfile("run.cmd")` turned out to be a
  silent no-op in some session contexts. The service now relaunches itself
  as a detached `pythonw` process directly — no `.cmd` wrapper, no flashing
  console. Proven end-to-end in a sandbox: an old install finds the release,
  applies it and comes back up on the new version by itself.

## 0.1.6 — 2026-11-20

### Fixed
- **Auto-update restart actually restarts.** The first real update cycle
  (0.1.4 → 0.1.5) applied fine but the process exited before the delayed
  relaunch fired — the relaunch thread was a daemon and died with the
  process. It is non-daemon now, so `run.cmd` really starts the updated
  service.

## 0.1.5 — 2026-11-20

### Fixed
- **Crisp text at Windows scaling >100%** (150% is the common laptop default):
  the service and the installer's model dialog are now DPI-aware — Tk renders
  natively instead of a stretched bitmap that made all labels blurry.
  Fixed-pixel sizes (slider lengths, note wrapping) scale with the DPI factor.
- The settings drop-downs are dark-themed now (they were glaring white) and
  the small helper notes are brighter and slightly larger.

## 0.1.4 — 2026-11-20

### Added
- **Model choice at install**: without an explicit `--model` the installer
  shows a Tk dialog — four models with size and description, Install/Cancel
  (Cancel stops the installation cleanly). Fallbacks: a console menu when a
  tty is attached but Tk is unavailable; the default `large-v3-turbo` silently
  on an unattended run. On an existing install the configured model is kept.
  `install.ps1` no longer pre-selects a model (`-Model` stays optional).
- **Model switching in settings**: the *Model & language* tab has the model
  selector (with a per-model description) and the "keep the previous model
  on disk" switch (`model_keep_old`, default on). Switching downloads the
  new model first, reloads the service without a restart and — only when
  keep-old is off — deletes the previous model from the service folder; a
  model in the shared Hugging Face cache is left there (honestly noted in
  the dialog). A failed download reverts to the previous model.
- **Switch progress dialog**: phases download → load → (remove) → done,
  with a close button at the end; a dictation cycle in progress reports
  "busy" instead of switching.
- Settings window reorganized into tabs — Recording / Model & language /
  Appearance / System (ttk.Notebook, dark theme) — the window no longer
  clips at the bottom.

## 0.1.3 — 2026-11-20

### Added
- Auto-update: a background check for a newer GitHub release every 2 h by
  default (first pass ~90 s after startup, only while idle). A newer release
  is downloaded and installed into the same folder, then the service
  restarts itself.
- Settings section "Updates": auto-check on/off, interval (1–24 h), and a
  "Check now" button with a visible result.
- The startup log line includes the service version.

## 0.1.2 — 2026-11-20

### Added
- **One-command install**: `irm https://raw.githubusercontent.com/FormaMindPro/vibeforma/main/install.ps1 | iex`
  — the bootstrap downloads the latest release (or the main branch), unpacks
  it and runs the installer; options via
  `iex "& { $(irm …) } -Model large-v3 -NoRun"`.

### Changed
- Languages in the tray and settings are written as endonyms — every
  language in its own language (English, Русский, Українська, Deutsch,
  日本語…). Codes are still stored in config.json.

## 0.1.1 — 2026-11-20

### Changed
- Recognition language is **pinned exactly** — no per-phrase guessing. The
  model recognizes exactly the chosen code; `auto` removed from the tray and
  settings selectors. Product default: `en` (was `auto`). A legacy `auto`
  value in config.json maps to `en`.

## 0.1.0 — 2026-11-20

First international release.

- Local voice dictation for Windows: hotkey → speak → text inserted into the
  focused field (clipboard + layout-independent Shift+Insert, old clipboard
  restored)
- Recognition language: auto-detection per phrase or a pinned code (26 codes
  in the UI); per-language prompt presets; user terms via `hotwords`
- Interface localization: English and Russian (`app/locales/*.json`,
  drop-in files for more languages)
- Monitoring panel: mic level, Silero VAD, near-real-time streaming partials,
  transparency slider, autogrow
- Settings window applied live (no restart): silence pause, streaming
  interval, max recording, paste method, sounds, hotkey, input device,
  autostart
- Tray menu: pause, input device, model switch without restart, recognition
  language, logs folder
- Models: large-v3 / large-v3-turbo / medium / small; models live in
  `models/` inside the service folder or the shared Hugging Face cache;
  offline-first loading (network only for a first download)
- Installer `install.cmd` / `install.py`: venv → dependencies → model →
  shortcuts (branded icon) → launch; flags `--model`, `--target`,
  `--dry-run`, `--cpu`, `--no-run`
- CUDA float16 on NVIDIA GPUs, automatic CPU (int8) fallback
- Single-instance lock, rotating logs, machine selftest (`run.cmd --selftest`)
