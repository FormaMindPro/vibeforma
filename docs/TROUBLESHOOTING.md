# Troubleshooting

Start with the built-in report: it lists every check with `OK`, `WARN` or
`FAIL` and never includes dictated text or window titles.

- Command line: `run.cmd --doctor` (exit code 0 — nothing failed, 1 — a
  check failed).
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

## File transcription does not start

A file job needs the service running: `run.cmd` starts it, and
`run.cmd --status` shows whether it answers.

- **`VibeForma is not running`** — the CLI asks the current user's named pipe
  (`\\.\pipe\VibeForma-<user>`); start the panel from the tray or `run.cmd`
  and retry. The exit code is 1.
- **`path is outside the allowed roots`** — an agent-named path must sit
  inside `file_roots` (an empty list means local fixed/removable disks only).
  Network shares, UNC paths and device paths are refused with 403; the tray
  dialog is not bound by this rule because the person picks the file.
- **`file is larger than N MB` / `file is N s, longer than file_max_s=...`**
  — raise `file_max_mb` or `file_max_s`. The duration cap is checked from
  the container's declared duration before decoding, or as soon as an unknown
  duration crosses it; a synchronous `/v1/audio/transcriptions` call is capped
  by `file_max_sync_s` (900 s) and a longer file must go to `POST /jobs`.
- **A dictation waits for a file block** — the file mode preprocesses audio
  in blocks of at most 600 s (`FILE_CHUNK_S` in `app/asr.py`); a dictation
  that starts mid-file waits for the block in flight, never for the whole
  file. Over-long files are refused before decode, so they cannot occupy the
  engine for hours.
- **`the model is loading` (CLI exit 1)** — the service is up, the pipe
  answers, and the first model load is still running. Retry in a few seconds;
  `location.json` names the pipe only once it listens, and its `file_mode` is
  `false` until then.
- **`already exists; pass --force`** — the result file is not overwritten by
  default; pass `--force` or choose another `--out`.
- **Unsupported codec** — PyAV covers the usual containers (mp4, mkv, mov,
  webm, mp3, wav, m4a); an exotic codec is reported as a job error with the
  file name and no transcript text. No external ffmpeg is required or used.
- **Exit code 2** — an option or format is wrong; `run.cmd --help` prints
  the contract. **Exit code 3** — the job was cancelled.

## Batch folder transcription

- **Exit code 4 / `nothing to transcribe`** — the folder holds no supported
  audio or video file at that level. Supported: mp4, mkv, mov, webm, mp3,
  wav, m4a, m4b, ogg, flac. Add `--recursive` when the files are in
  subfolders; an unsupported file is not an error, it is simply not found.
- **`folder is outside the allowed roots`** — a CLI-named folder must sit
  inside `file_roots` (an empty list means local fixed/removable disks only).
  The tray dialog is not bound by `file_roots` because the person picked the
  folder; network, UNC and device paths are refused in both cases.
- **`duplicate output target: <path>`** — two sources in one folder have the
  same stem (`a.mp4`, `a.mkv`), so both would write `a.srt`. Rename one or
  put them in different folders; `--force` does not resolve this conflict,
  it only allows overwriting files already on disk.
- **A file is skipped** — its outputs already exist beside the source (or
  under `--out`). This is a successful outcome and the code stays 0; pass
  `--force` to overwrite.
- **One file failed, the batch went on** — the summary names it in
  `summary.files[]` with the state `error`; the rest of the batch finished.
  The CLI exits 1 once the batch ends.
- **Cancel a batch** — tray → **Cancel batch**, or `run.cmd --cancel-batch
  [<batch-id>]`. The file in flight stops between segments; no partial output
  is left behind. A second cancel answers 1 because nothing is left to stop.
- **`the model is loading` / `VibeForma is not running`** — the same rules as
  a single file: the service must be running, and the first model load makes
  the CLI answer `the model is loading` with code 1.

## Not enough disk space

The installer needs the program plus a first model. Models: `small` 0.5 GB,
`medium` 1.5 GB, `large-v3-turbo` 1.6 GB, `large-v3` 3.1 GB. `--doctor`
prints free space on the data drive. A model in the shared Hugging Face
cache is not downloaded again.

## Windows warns about the install command

The one-command install from the README downloads the release zip from
GitHub and runs `install.py` from it. SmartScreen or the PowerShell policy
may ask for confirmation; that is the normal warning for any script
downloaded from the internet. Nothing else is downloaded.

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

- An update runs the same GitHub installation command as the README. When
  it fails, the log shows the command's exit code and the tail of its
  output; the current version keeps working.
- `auto_update` defaults to `notify`: the tray offers **Update to …** and
  **What's new in …** (release notes). Only an explicit opt-in installs
  without asking.
