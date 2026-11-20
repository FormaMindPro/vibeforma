# Troubleshooting

Start with the built-in report: it lists every check with `OK`, `WARN` or
`FAIL` and never includes dictated text or window titles.

- Source install: `run.cmd --doctor` (exit code 0 — nothing failed, 1 — a
  check failed).
- Packed build: `VibeForma.exe --doctor`; because the exe has no console the
  report is also written to `%TEMP%\vibeforma-doctor.txt`.
- Running app: tray menu → **Diagnostics…**, or Settings → **System** →
  **Diagnostics**; the window has a **Copy report** button for a support post.

The log lives in `%LOCALAPPDATA%\VibeForma\logs\service.log` (source install:
`app\logs\service.log`). It rotates through five 1 MB files and holds no
dictated text and no window titles by default.

## The microphone hears nothing

- Open the tray menu → **Input device** and pick the microphone you speak
  into. The panel shows a silence warning when the level stays near zero
  while recording.
- `--doctor` prints the number of input devices and the default one; no
  input devices means Windows does not see a microphone (check Privacy →
  Microphone and the device driver).
- If `config.json` names a device that is gone, VibeForma falls back to the
  system default; the report marks this as a warning.

## The HTTP API does not answer

The local API is off by default. With `"http_api": true` it needs a
non-empty `"http_token"` and listens on `127.0.0.1` only.

- **Port already in use** — the log says `HTTP API not started: [WinError
  10048]`; the dictation service itself keeps working. Change `http_port`
  in `config.json` or turn `http_api` off.
- **401** — the `X-VibeForma-Token` header is missing or wrong.
- **415 / 411 / 413** — the body must be `Content-Type: audio/wav` with a
  WAV file up to 10 MB.
- **429** — one request per second.
- **409** — the service is not idle (recording, recognizing or updating).

## Not enough disk space

The installer needs the program plus a first model. Models: `small` 0.5 GB,
`medium` 1.5 GB, `large-v3-turbo` 1.6 GB, `large-v3` 3.1 GB. `--doctor`
prints free space on the data drive. A model in the shared Hugging Face
cache is not downloaded again.

## Windows SmartScreen warns about the setup

The packed setup is signed in CI; SmartScreen may still show "Windows
protected your PC" until a file earns reputation. Before installing, check
the signature in PowerShell:

```powershell
Get-AuthenticodeSignature .\VibeForma-Setup-<version>.exe | Format-List Status, StatusMessage, SignerCertificate
```

`Status: Valid` and a publisher certificate mean the file is intact and
signed. Do not install a setup whose signature is invalid. The one-command
install from the README does not need the setup at all. Details:
[INSTALL-WINDOWS.md](INSTALL-WINDOWS.md).

## The model does not load

- `--doctor` shows `device` and `compute_type`. `device=cuda` with no CUDA
  device is a failed check: reinstall with `--cpu` or set `"device": "cpu"`,
  `"compute_type": "int8"` in `config.json`.
- A model that is not on disk yet downloads on the first start; the report
  marks it as a warning.
- A failed download leaves the previous model in place. Remove the
  incomplete `%LOCALAPPDATA%\VibeForma\models\<name>` folder and restart.

## The hotkey does nothing

- The report validates the combination. A combination reserved by Windows
  is a warning, not a refusal — it may still work depending on the system.
- Change it in Settings → **System** → **Hotkey** (or in the setup wizard).
  A failed start keeps the previous combination and shows a notification.

## The text was not pasted

The active window changed while you spoke. VibeForma asks where to put the
text; **Copy** leaves it in the clipboard and the tray says so. Turn off
"ask where to paste" in Settings → **System** to copy silently in that case.
Pasting defaults to Shift+Insert, which is layout-independent.

## The update is not applied

- Updates are verify-first: the release manifest is signed and checked
  before any file is replaced; a release without a valid signature is
  refused and the log names the reason.
- `auto_update` defaults to `notify`: the tray offers **Update to …** and
  **What's new in …** (release notes). Only an explicit opt-in installs
  without asking.
- A packed installation updates through the next signed setup or winget,
  not through the zip updater — the app says so instead of replacing its
  own files.
