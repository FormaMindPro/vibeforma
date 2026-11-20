# Changelog

All notable changes to VibeForma are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- **A packed Windows setup, signed and fail-closed.** `tools/build_installer.py`
  builds a one-folder PyInstaller application (no Python on the target) and a
  per-user Inno Setup installer: English or Russian wizard, Start menu, an
  optional desktop shortcut and logon start, an in-place upgrade and an
  uninstaller that keeps settings and models unless asked. The packed app
  keeps user data in `%LOCALAPPDATA%\VibeForma`, so an upgrade replaces the
  program only; a packed installation upgrades through the next signed setup
  (or winget) and the zip updater refuses instead of touching its own files.
  The build runs the exe's own `--bundle-check`, signs with signtool when
  `VIBEFORMA_CERT_*` is set and stops instead of shipping an unsigned file;
  the release workflow builds and signs the setup and adds its hash to the
  signed release manifest. `--winget` writes the four winget manifests with
  the real hash. `docs/INSTALL-WINDOWS.md` covers SmartScreen, signature
  verification, the data folder and upgrades.
- **An error catalog, a Russian installer, calmer settings.** Every UI
  error — microphone, model download or load, recognition, a bad dictionary
  file — now comes from `app/errors.py`: a plain message plus a next action,
  localized; the raw exception text goes only to the log. The installer
  speaks Russian on a Russian Windows (English fallback) in the setup
  dialog, the console menu and the final line. Settings gained a search
  field, a Reset button (the model, dictionary and history are kept), Save
  no longer closes the window and confirms in place, and a model switch
  shows its progress inside the settings window. The Russian docs use one
  term for the panel.
- **Dictionary and history.** A Dictionary tab keeps term -> replacement
  pairs: terms go into the recognition prompt, replacements are applied
  after recognition (case-insensitive, longest term wins), and JSON import /
  export plus a sample preview make the list easy to check. An opt-in
  History tab stores the last 100 dictations locally in `history.jsonl`
  with copy, paste again, delete and clear; turning it off deletes the file,
  and the dictated text is never written to the log. "Undo last insert"
  sends Ctrl+Z by virtual key only to the same window within 30 s and warns
  instead otherwise; "Restore clipboard" returns the clipboard from before
  the dictation.
- **The installer is a transaction.** Before writing anything it checks
  Windows 10 build 1809+, x64/ARM64, Python 3.10–3.14, free space
  (model + 4 GB) and the target folder (empty, an existing install, or a
  folder marked by `.vibeforma-install.json`). System folders, Program
  Files, drive roots and the profile root are refused. The first failed
  step stops the chain — no shortcuts, no config, no launch; a failed first
  install removes its folder, an existing install is restored from a
  backup. Every step, command and error tail goes to `install.log`
  (`%LOCALAPPDATA%\VibeForma\logs`, `--log` overrides), and the console
  prints the path. A rerun continues from the failed step, reusing the
  venv, model and config already in place.
- `install.ps1` installs a released build only: without a release zip it
  stops with an explanation instead of falling back to the main branch.
- **CI and a test suite.** GitHub Actions runs the tests on Windows and the
  platform-independent subset on Ubuntu (`-m "not windows"`), plus a ruff
  gate. The 34 tests need no model, GPU or network: config load/save and
  atomic replace, en/ru locale key and placeholder parity, version-to-changelog
  sync, `updater.version_tuple` and release parsing, clipboard insertion with
  substituted clipboard/keyboard modules, the installer with a substituted
  `subprocess`, and the HTTP API guards (loopback, idle, known path).
- `requirements-dev.txt` with pinned `ruff`, `pytest` and `pytest-cov`.
- `tools/ruff_gate.py` and `tools/ruff-baseline.json`: the 164 findings of
  0.1.9 are a recorded ceiling; any new finding fails CI, while fixing the
  old ones stays a separate cleanup task.
- README badges and a local test command.
- **Verified, consent-based updates.** A release now ships a signed
  `release-manifest.json` (minisign / Ed25519) and `SHA256SUMS`; the updater
  checks the signature, the zip hash and a signed file whitelist before any
  installer code runs, and refuses an unsigned release. `auto_update` defaults
  to `notify`: the tray offers Update / Later, and only an explicit opt-in
  installs without asking. The new `updating` state blocks the hotkey and
  recording. Before replacing files the updater backs them up and restores the
  previous version if the installer fails.
- `tools/build_release.py` and `.github/workflows/release.yml`: build the zip
  with `test_ru.wav`, write the manifest and `SHA256SUMS`, sign the manifest in
  CI, and fail closed when the signing secret is absent.
- `test_ru.wav` is now part of the repository and the release; `install.py`
  copies it and gained `--selftest`.
- **A CPU profile: the installer fits the machine.** Base dependencies come
  from `app/requirements.txt`; the CUDA wheels (`nvidia-cublas-cu12`,
  `nvidia-cudnn-cu12`) are installed from `app/requirements-cuda.txt` only
  when CUDA is detected, so a CPU install no longer downloads them. On CPU
  the dialog preselects the `small` model and `config.json` gets
  `device: "cpu"`, `compute_type: "int8"`; an existing config that still
  says `cuda` on a machine without CUDA is corrected. The recognition
  language defaults to the Windows locale (`en` when no code matches) and
  `--language` overrides it.
- **Migration from WhisperDictation.** When the old
  `%LOCALAPPDATA%\WhisperDictation` folder is found, its `config.json`
  (with `hotwords`) and downloaded `models` are copied into the new install,
  and its desktop/startup/Start menu shortcuts are removed, so two copies
  never autostart. The old folder is copied, not deleted.
- **Uninstall.** `uninstall.cmd` / `uninstall.py` (stdlib) remove the
  shortcuts, the startup entry, the Settings > Apps record and the service
  files; models and `config.json` are kept unless `--remove-data`, so a
  reinstall starts offline. `Settings > Apps` and the Start menu now list
  VibeForma; the version is shown in the settings window.
- **A three-step first-run wizard.** Language (interface and recognition),
  microphone (live level and a test phrase whose recognized text is shown,
  never inserted), and the hotkey recorded by pressing it. The hotkey is
  checked with `pynput.keyboard.HotKey.parse`; modifiers alone are refused,
  a Windows-reserved combination is a warning. "Skip" writes safe defaults;
  the wizard reopens from the tray and from Settings > System, and the new
  `onboarding_done` flag (default `false`) remembers that it ran.

### Note
- The production signing key is not embedded yet (`RELEASE_PUBLIC_KEY` is
  empty), so real updates are refused until a maintainer adds the public key
  and `MINISIGN_SECRET_KEY` to the repository secrets. This is deliberate:
  no unsigned update is applied.

### Changed
- **Code hygiene: the service package, typed modules, blocked lint, pinned
  dependencies.** `app/main.py` is a 46-line entry shim over the new
  `vibeforma` package (`service`, `actions`, `settings_ops`, `update_ops`,
  `http_api`, `updates`, `selftest`, `app`, `util`, `base`); the installer
  is a flow plus `install_core`, `install_env`, `install_steps` and
  `install_system`; `SettingsWindow.__init__` builds the window through
  named step methods; the model catalog lives in one module. Strict mypy
  covers the package in CI. Ruff is blocking with the whole recorded set
  fixed (blind catches in UI, ctypes and PortAudio boundaries are narrowed
  or suppressed with a reason; `%` formatting is gone). The service venv
  installs from hashed locks (`app/requirements.txt` and
  `requirements-cuda.txt`, generated from `requirements.in` /
  `requirements-cuda.in` with `uv pip compile --generate-hashes`), split
  base / CUDA as before.
- **Insertion knows its target and leaves your clipboard alone.** When the
  window that was active at the hotkey press is no longer in front,
  VibeForma no longer steals the focus: it asks "Paste into the current
  window / Copy / Cancel" (30 s without an answer means Copy; without a
  dialog the text is only copied). The previous clipboard is restored only
  when `GetClipboardSequenceNumber` shows that nobody wrote to it in the
  meantime — an empty clipboard is restored too (it used to be skipped) —
  and the restore can be turned off in settings.
- On Windows 10+ the dictated text is marked as excluded from the clipboard
  history and cloud sync (`ExcludeClipboardContentFromMonitorProcessing`,
  `CanIncludeInClipboardHistory=0`, `CanUploadToCloudClipboard=0`); the
  settings note states that other clipboard managers may still keep it.
- **Notifications no longer show the dictated text by default** — "Inserted,
  N chars"; the phrase appears only with the new "show the dictated text in
  notifications" opt-in. A tray message explains when the text was left in
  the clipboard because the window changed.
- New settings (System tab): ask on a focus change, exclude from the
  clipboard history, show the dictated text in notifications. Defaults keep
  the private behavior: ask, exclude, hide.
- The service window and launcher are titled VibeForma instead of
  WhisperDictation; shortcuts are created on the desktop, in the Start menu
  and in startup.
- Settings refuse an invalid hotkey instead of saving it; a changed hotkey
  starts the new listener before the old one stops, and a failed start keeps
  the previous combination with a visible notification.

### Security
- **The log no longer names windows or quotes dictation.** The service used
  to write the paste target's window title at recording start and after each
  paste; it now records only the character count and the window handle.
  `log_window_titles: true` brings the title back for a diagnosis. The
  dictated text was never logged and still is not.
- **The local HTTP API needs a token and is bounded.** With `http_api` on,
  `http_token` is mandatory — the server refuses to start without it; every
  request needs `X-VibeForma-Token` (constant-time compare),
  `Content-Type: audio/wav`, a body up to 10 MB and at most one request per
  second. The request moves the service to `processing`, and the temporary
  WAV (`_http_in_*.wav`) is deleted in `finally`, success or failure.
- **The panel has a privacy mode** (the eye button): the streaming and last
  text are hidden, the mic level stays, and the status line disappears 30 s
  after it was set. `hud_privacy: true` starts the panel in this mode.
- README (en/ru) gained "What stays on disk" and the HTTP API contract.

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
