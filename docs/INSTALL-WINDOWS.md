# Installing VibeForma on Windows

The Windows setup installs the app for the current user: no administrator
rights, no Python, no build tools. Recognition models are downloaded on the
first run and live in your user profile.

## Requirements

- Windows 10 version 1809 or newer, or Windows 11, x64.
- About 2 GB free for the program and a first model; larger models take more.
- A microphone. A GPU is optional: without CUDA the app uses the CPU.
- The first run needs the network once, to download the model. After that
  dictation works offline.

## Install

1. Download `VibeForma-Setup-<version>.exe` from the latest release.
2. Run it. The wizard offers English or Russian and installs into
   `%LOCALAPPDATA%\Programs\VibeForma`.
3. Shortcuts: Start menu by default, desktop and "start at logon" optional.
4. On the first launch, choose the language, check the microphone and record
   the hotkey (default `Ctrl+Alt+Space`).

The installer closes a running copy before replacing files ("Close
applications" is on), so an upgrade can be started from the app itself.

## SmartScreen and the signature

Windows SmartScreen shows "Windows protected your PC" for an executable it
has not seen before. A code-signing certificate does not remove the
reputation warning instantly, but it replaces "Unknown publisher" with the
publisher name and makes the file verifiable.

Check the signature before installing (PowerShell):

```powershell
Get-AuthenticodeSignature .\VibeForma-Setup-0.1.9.exe | Format-List Status, StatusMessage, SignerCertificate
```

`Status: Valid` and a `SignerCertificate` issued to the publisher mean the
file was signed and has not been modified since. A release without a valid
signature should not be installed.

The setup is built and signed in CI with `tools/build_installer.py --sign`;
the build fails when the signing certificate is not configured, so no
unsigned setup is published by the release workflow.

## Where the data lives

| Path | What |
| --- | --- |
| `%LOCALAPPDATA%\Programs\VibeForma` | program files (replaced by an upgrade) |
| `%LOCALAPPDATA%\VibeForma\config.json` | settings |
| `%LOCALAPPDATA%\VibeForma\models` | downloaded models |
| `%LOCALAPPDATA%\VibeForma\logs` | rotating service log (no dictated text) |
| `%LOCALAPPDATA%\VibeForma\history.jsonl` | only when history is turned on |

The program folder and the data folder are separate on purpose: an upgrade
or a reinstall replaces the program and keeps settings, models and history.
The uninstaller keeps the data folder unless "Remove settings, models and
history on uninstall" is ticked.

## Upgrade

- Run the newer setup over the installed version: files are replaced in
  place, the data folder is untouched.
- `winget upgrade FormaMindPro.VibeForma` when the package is published to
  winget.
- A packed installation does not apply the zip update used by a source
  install: it upgrades through the signed setup or winget, and the app says
  so instead of replacing its own files.

## Uninstall

Settings > Apps > VibeForma, or the Start menu entry "Uninstall VibeForma".
Shortcuts and the program folder go; user data stays unless the uninstall
option is ticked.

## Build the setup yourself

```powershell
python -m pip install -r requirements-build.txt
python tools/build_installer.py --dry-run   # show the steps
python tools/build_installer.py             # packed app + setup (unsigned)
```

`--sign` needs `VIBEFORMA_CERT_PFX` + `VIBEFORMA_CERT_PASSWORD` or
`VIBEFORMA_CERT_THUMBPRINT`; without them the build stops. `--winget`
writes the four winget manifests with the real hash into `dist\winget`.
Inno Setup 6 is required for the setup step.
