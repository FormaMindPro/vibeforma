# Changelog

All notable changes to VibeForma are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
