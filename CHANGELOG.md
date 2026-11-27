# Changelog

All notable changes to VibeForma are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.5.3 — 2026-11-27

### Fixed
- **A busy clipboard no longer eats the phrase.** When another program held
  the clipboard for a moment, the copy of the recognized text threw and the
  take died before any paste. The copy now retries for a short moment and,
  if it still fails, ends the insert with an explicit error status; the
  text stays in the dictation history for a manual re-insert.
- **Holding `Esc` cancels once.** Every autorepeat press of a held `Esc`
  used to spawn its own cancel thread — dozens of parallel cancels and
  repeated notifications. The watcher now keeps a single cancel in flight
  and ignores autorepeat presses until it finishes.
- **Recording stops safely and the sound monitor comes back.** Stopping a
  take no longer races the audio writer; the live system-audio monitor
  revives itself after a device failure, and a partial iteration failure
  is reported honestly.
- **Hand-written hotkey aliases start the service.** A `config.json` value
  like `control+alt+space` or `win+escape` used to kill the start; one
  shared keymap now normalizes the aliases for the hotkey and the
  onboarding window alike, with reserved-combo warnings unchanged.
- **The installer survives Cyrillic and special-character paths.** Dollar,
  backtick, quote and wildcard-bracket characters in the target folder are
  escaped, child output is read as UTF-8, `~/VibeForma` expands to the home
  folder, a broken locale placeholder can no longer crash an error message,
  and a CUDA driver mismatch gives its own hints.
- **A failed update rolls back to the old installation.** The previous
  version marker is snapshotted into the backup before anything overwrites
  it, the backup survives a failed restore with its path written to the
  log, and the virtual environment is rebuilt from the restored
  requirements.
- **Uninstall finds shortcuts at odd paths.** Shortcut lookup escapes the
  `-like` wildcard metacharacters, so shortcuts are removed even when the
  install path contains `$`, a backtick, quotes or square brackets.

### Changed
- CI measures coverage on a Python matrix (3.10–3.14 on Windows, 3.12 on
  Linux) with a `fail_under` threshold, and the root modules are covered by
  strict mypy; the installer stays a thin entry under 400 lines.

## 0.5.2 — 2026-11-26

### Fixed
- **Insertion no longer races the hotkey.** Stopping a dictation with the
  same combo that started it could send the paste while Ctrl and Alt were
  still physically held: the window received Ctrl+Alt+Shift+Insert, which no
  app treats as paste, and the phrase silently stayed on the clipboard while
  the log said "Pasted" (measured 2026-11-26 in Chromium: Shift+Insert pastes,
  Ctrl+Alt+Shift+Insert does not). VibeForma now waits for the combo to come
  up — at most one second — and checks the target window again after the
  wait; when the wait times out the text is deliberately left on the
  clipboard as the manual fallback. The same wait covers the selected-text
  read at the start of a take and the undo keystroke. The previous clipboard
  is restored after a successful paste again: the sequence number used to be
  read before the private-format writes of the same call, so every restore
  looked like a foreign change and was skipped.

## 0.5.1 — 2026-11-26

### Added
- **A system-audio take is recorded to files.** With the sound source set to
  **System audio**, the take is written as a 16-bit, 16 kHz mono WAV into
  `system_recordings_dir` — empty means `Documents\VibeForma\Recordings`, a
  date subfolder, the file named by the take's start time — and on stop TXT
  and SRT are queued beside it through the file queue. The path is shown on
  the panel, in the tray and in the HUD. Nothing is inserted, and the
  dictation history and statistics stay untouched. A folder that cannot be
  written is reported before the take starts, and the live subtitle buffer
  stays at 30 s. Limits: a take longer than `file_max_s` (6 hours) or larger
  than `file_max_mb` (4 GB) keeps its WAV but gets no transcript; `Esc` and
  exit close the WAV without queueing the transcript.
- **A take clock on the panel.** A monospaced clock sits in a fixed-width
  column left of the level bar and appears only during a take. A microphone
  take with a limit (`continuous: false`) counts down to `max_record_s` with
  a minus sign and turns amber under 30 seconds and red under 10; continuous
  dictation and a system-audio take count elapsed time in neutral color.
  Hovering says which time it shows, and the digits never shift the line
  below. The limit is fixed when the take starts; a silence auto-stop can
  still end a limited take earlier.
- **A complete uninstall.** `uninstall.cmd --remove-data` removes what the
  installer and the application created — the `vibeforma\` package,
  `mcp.cmd`, `requirements.in`, `requirements-cuda.in`, history and its
  sound, statistics, `status.json`, `location.json`, corrections, the
  top-level `.py` files, `__pycache__` and `.venv` — and leaves the folder
  empty or removed, which the installer accepts as a fresh install.
  `--keep-data` keeps models, configuration, logs, history, statistics and
  corrections; files that belong to you stay and are named on screen. The
  removal stays inside the install folder and removes a symbolic link or a
  junction as a link, never following it out.
- **Local model folders in Settings.** The **Model & language** tab lists
  CT2 model folders with their name, path and a validity mark computed every
  time the list is drawn (`model.bin` present, so a folder that lost it turns
  invalid instead of keeping a stale mark); the current model is marked.
  **Add folder** picks a directory and refuses one without `model.bin` with a
  clear message; **Use** switches through the existing progress dialog, and a
  failed switch keeps the model that was in use; **Remove from the list**
  deletes only the record and never a file (the current model's record is
  kept with an explanation); **Return to the built-in** selects a catalog
  model again. Switching away from a local folder never deletes its files,
  even with `model_keep_old=false`; downloaded models inside `models\` are
  still removed as before. The list lives in `config.json` under
  `model_folders` as `{name, path}` records, survives a restart, and a manual
  path in `model` keeps working; an old config without the key reads as an
  empty list.
- **Recognition quality presets.** The **Recording** tab offers **Normal
  (recommended)** — the final dictation pass's beam 5 with the 0.0 → 0.2
  fallback ladder; **Accurate** — beam 10 at temperature 0, slower; and
  **Fast** — beam 1 at temperature 0, quicker but weaker on a complex
  phrase. The choice is stored as `quality_preset` in `config.json`: an old
  config without the key and an unknown value both read as Normal, and the
  choice applies to the next final dictation pass without a restart. File
  transcription and the live system-audio subtitles keep their own settings.
  The settings panel exposes no raw temperature for this pass: the preset is
  its only source of beam and temperature.
- **"Recognize more precisely" for one history entry.** The **History** tab
  offers the button for the selected entry while its dictation WAV is still
  on disk; without the sound the button stays disabled with a hint, and a
  file that disappeared after the row was drawn is refused before anything
  runs. The pass runs in the background with a temporary ASR and the same
  wide search as the draft retry (beam 10, temperature 0), so the table
  stays usable and a progress bar is visible while it runs. The new engine
  text and the pass's confidence reading (the mean word probability) replace
  the entry's `text` and `confidence` in `history.jsonl`; the row is redrawn
  with the new text and the entry stays selected. The dictionary, text
  rules, snippets and voice actions are not re-applied — only the engine's
  own text changes, as in the draft retry. A failed, empty or unreadable
  pass leaves the entry exactly as it was and shows a localized reason.

## 0.5.0 — 2026-11-25

### Added
- **Live subtitles of system audio.** Switch the sound source to **System
  audio** and the same hotkey starts a take that shows the last one or two
  phrases in the panel instead of inserting text. The take has no auto-stop
  on silence and no 120 s cut, keeps a 30 s buffer and refreshes the plate
  no more often than every 4 s; the same hotkey stops it. Nothing reaches
  the active window, the draft, the history or the statistics. Two live
  takes of 409.5 s and 844.4 s held. Known limits: the subtitle filter
  drops only short stop phrases and repeated loops, so invented text over
  music can still reach the plate; the system mode does not write an audio
  or subtitle file yet, and that is the next round.
- **A doubtful draft offers "Recognize more precisely".** When the final
  pass ran with `temperature > 0` or the phrase looks uncertain, the panel
  keeps it as a draft, underlines the doubtful words and offers **Recognize
  more precisely** and **Restore previous**. The retry runs the same
  recording through the same loaded model with beam 10, temperature 0 and
  word timestamps; it replaces only the engine text — dictionary, rules,
  snippets and voice actions are not re-applied — and **Restore previous**
  returns the exact previous text and marking. The offer needs the take's
  WAV, so a long take in continuous mode without history sound cannot be
  retried; the rollback is one level.
- **Optional text cleanup.** `text_cleanup` (off by default, no settings tab
  yet) turns numbers and dates spoken as words into digits for ru and en
  through `wetext` 0.2.1 (Apache-2.0, installed with the app), removes a
  curated list of ru and en filler words as whole words only, and
  capitalizes sentence starts. Protected fragments — code, snake_case and
  camelCase names, dotted paths, URLs, e-mail, Windows paths and backticked
  text — stay byte for byte. The step runs after the dictionary, text rules,
  snippets and voice actions, before the draft and the insertion;
  punctuation stays Whisper's. Limits: a lone small numeral outside a
  recognized phrase stays words (wetext's own rule), and a wetext failure
  leaves the words as said and is not logged.
- **Application profiles.** `app_profiles` ties a profile to a process name
  and/or a window-title fragment; a profile carries its own dictionary and
  snippets. The profile of the window that will receive the text — the one
  active at the hotkey — applies, not the window active when recognition
  ends; profile rules merge over the global ones without duplicates, and
  other windows keep the global rules. The **Dictionary** tab has an
  **Application profiles** section: the list, add/edit/remove, the profile's
  terms and snippets, save and reset; the limit is 50 profiles.
- **History with sound and re-recognition of the last take.**
  `history_audio` (off by default) keeps a WAV of each dictation in
  `history_audio\` next to `history.jsonl` when `history` is on too, under a
  200 MiB cap; deleting a record or clearing the history deletes its WAV,
  and turning the option off removes the stored sound. The tray item
  **Re-recognize the last dictation** asks for a prompt (prefilled with
  `hotwords`), runs a temporary ASR with the current or another model and
  leaves the new text in the panel draft.
- **Voice actions.** `actions` in `config.json` — a whole dictated phrase,
  matched after the dictionary and snippets — runs `hook_command`, switches
  the model or runs its own command for an external agent. A matched action
  is not inserted as text; a failed command leaves the normal insertion and
  writes a status line with the exit code to the log. The phrase still
  counts in the statistics. Config-only for now.
- **Dictation context, opt-in.** `dictation_context` (off by default) sends
  the previous phrase (a 160-character tail) and the text selected in the
  target window (up to 400 characters, read through Ctrl+Insert) into the
  window's hotwords, so a name keeps its spelling; with the option off
  nothing is read. Config-only.
- **Punctuation presets for all 26 menu languages.** The recognition prompt
  preset now covers every language code in the menu (it covered six), each
  written in its own script and alphabet.
- **A language hint.** After the final pass the panel can say "looks like X
  (..%), Y is pinned (..%)" for 20 s when the pinned language's probability
  is below 0.5 and another language leads with at least 0.5. Recognition
  stays on the pinned language; a failed detection or an English-only model
  simply gives no hint.
- **A CT2 model from a folder.** `model` accepts a folder path as well as a
  catalog name or a Hugging Face repo id. The folder is loaded with
  `local_files_only=True` and checked structurally — `model.bin` with its
  CT2 header, `config.json` as a JSON object, `tokenizer.json` and the
  vocabulary; a broken folder raises a named error instead of silently
  downloading the catalog model.
- **Sentence captions in SRT and VTT.** With word timestamps on, cues are
  cut by sentences: a sentence end or a pause of 0.6 s or longer closes a
  cue, a shorter pause glues words across engine windows and 600 s blocks; a
  segment without words stays its own cue; without word timestamps the
  engine segments stand. TXT and JSON are unchanged.
- **The agent contract reaches the engine.** `prompt`, `word_timestamps` and
  `clip_timestamps` are accepted by `/jobs`, `/v1/audio/transcriptions`, the
  CLI (`--prompt`, `--words`, `--clip`) and the MCP tools; a job reports the
  applied prompt and range. `verbose_json` follows the OpenAI schema with
  `segments[].avg_logprob`, `no_speech_prob`, `compression_ratio` and
  `temperature`, plus a top-level `words[]` with `probability`.
- **Confidence is no longer discarded.** Segment readings and word
  probability are published in JSON and `verbose_json`; a low-confidence or
  `temperature > 0` phrase waits in the panel draft with its doubtful words
  highlighted instead of a blind paste. Thresholds: `avg_logprob -1.0`,
  `no_speech_prob 0.6`, `compression_ratio 2.4`, word probability 0.6.
- **A local WER/CER reference for the engine.** `tests/wer/manifest.json`
  lists 24 short ru+en recordings with the correct text (names, numbers,
  terms, noise, music, silence) and `tools/wer_cer.py` measures WER and CER
  with `--self-test`, `--transcribe`, `--hyp-dir` and `--baseline`. The
  baseline `test_ru.wav` reads WER 0.00 % / CER 0.00 % on large-v3, CPU
  int8. The recordings themselves are not in the repository; the tool lists
  the missing files and exits 2 until they are recorded.

### Changed
- **Long dictation is continuous by default.** `continuous` (default true)
  lifts the `max_record_s` cut: the take is cut at a pause of at least 400 ms
  after at least 1 s of speech, or at 30 s with the last 0.3 s carried into
  the next chunk so a split word stays whole; chunks are recognized by the
  final pass in order, seams drop duplicated words and carry a sentence mark
  across, and the text is inserted once. Turning the option off restores the
  old stop at `max_record_s`; the system-audio take has its own path. When
  recognition falls behind and the receiver does not take a chunk, the buffer
  stops growing at 240 s: the buffered audio is dropped whole and the log
  carries a warning ("Continuous chunk dropped: ... while recognition is
  behind"). That is a memory guard, not the normal phrase limit — a very slow
  pass can cost up to those 240 s of audio instead of growing memory without
  bound.
- **The dictionary reaches every window.** Terms go to the `hotwords`
  parameter on dictation, the live partial, the sequential and the batched
  file path, not only to the first 30-second window; the recognition prompt
  carries the language preset only; terms use their target form and are
  selected by correction frequency within a 223-token budget. A term that
  does not fit is skipped whole, not cut mid-word, and each dictation window
  writes one INFO line with the number of terms.
- **File blocks continue each other.** A 200-character tail of the previous
  block joins the prompt of the next block on both the sequential and the
  batched path, so a name set in the first block survives into the second
  and third. The tail does not accumulate — the prompt length stays
  constant — and a gap in `clip_timestamps` resets it.
- **The batched file path is checked like the sequential one.** Every
  batched segment is measured against the same thresholds; a segment that
  fails is re-recognized sequentially over its own slice of the block, the
  replacement passes the same post-filter, and the failure and the retry are
  written to the log. The 1.35 `compression_ratio` threshold applies to
  English; other languages keep the library's 2.4 in this guard and the 1.35
  post-filter under `no_speech_prob`, because a clean Russian sample
  measures 1.466–1.476. On the reference phrase both paths read WER 0.00 %
  and CER 0.00 %.

### Fixed
- **The bootstrap hashes archives without `Get-FileHash`.** `install.ps1`
  computes SHA-256 through .NET, so the checksum gate works even when
  PowerShell inherits a module path without the Windows PowerShell utility
  modules.
- **The bootstrap unpacks through .NET `ZipFile`.** The archive is extracted
  into a fresh subfolder with `ZipFile.ExtractToDirectory`, avoiding the
  PowerShell `Expand-Archive` overload failure on some runners and keeping
  the downloaded checksum files out of the release tree.
- **A word-timestamp crash no longer fails a file job.** `IndexError` from
  faster-whisper's `find_alignment` (issue #1342) is caught at the block
  when word timestamps are on: the block is retried without them, the retry
  is logged, already delivered segments are not repeated, and the following
  blocks still ask for words. Without word timestamps the crash is not
  hidden and the job fails as before.
- **Invented text is filtered from files and system audio.** A stop list of
  typical ru and en false phrases (subscribe lines, "thanks for watching",
  subtitle credits, Amara.org and similar), a 2.0 s silence threshold with
  word timestamps, and a compression-ratio guard drop a segment when
  `no_speech_prob` is at least 0.6 and the phrase is a known fake or the
  ratio is above 1.35; a loop the engine still reads as speech stays and is
  flagged. The guard works on the sequential and batched file paths and in
  the final dictation pass; on the reference phrase it kept 17/17 words with
  no drops.

### Not included
- **Silero text enhancement.** Punctuation stays Whisper's: the Silero
  punctuation model is not part of 0.5.0. Its license is CC BY-NC-SA 4.0
  (non-commercial), torch and the model would add about 219 MB, the quality
  on Russian was not confirmed and there is no official ONNX build; the
  decision and the rejected options are recorded as ADR 0001 in the
  project's decision log. A commercial license or a punctuation model under
  MIT/Apache would reopen the question.

## 0.4.5 — 2026-11-24

### Added
- **An AI agent can use the local transcriber over MCP.** `app\mcp.cmd`
  starts a stdio MCP server that finds the running service through
  `location.json`, reads the token from `config.json` and offers three
  tools: `vibeforma_capabilities`, `vibeforma_transcribe_file` and
  `vibeforma_job_status`. Transcription queues the file through `/jobs` and
  returns the text when the job finishes. No listener is added and the
  requests are pinned to `http://127.0.0.1` with redirects refused.
- **Settings show the connection facts.** The new section **AI and
  automation** displays the loopback endpoint, the MCP launcher and two copy
  buttons: the key and a ready connection block for a local AI client. The
  key itself stays in `config.json`.

## 0.4.4 — 2026-11-24

### Added
- **The release archive is verified before anything is unpacked.** The app
  downloads `SHA256SUMS` together with the release zip, checks the hash and
  hands the verified local files to the bootstrap through
  `VIBEFORMA_UPDATE_ZIP` and `VIBEFORMA_UPDATE_SUMS`, never through the
  command line. `install.ps1` verifies the archive on its own download path
  too, and a missing sums file, a missing line or a mismatch stops the
  install before unpacking.
- **Updates wait for file work.** An update is deferred while a recording
  is in flight or `FileJobs` still has an unfinished single job or folder
  batch; in `auto` mode it retries by itself once the work completes. The
  deferred text and the notification name the recording or transcription.
- **Release notes come from the CHANGELOG.** The release workflow writes the
  current version's section into `RELEASE_NOTES.md` and attaches it as the
  GitHub release body, so a release is never published empty.

### Changed
- **A source checkout says so.** Running from a git checkout (a
  `pyproject.toml` beside `app/`, no install marker) shows a clear
  `source_checkout` state instead of installing a release zip over the
  sources; an installed copy updates as before.
- The updates documentation page describes the job-aware deferral and the
  checksum check before unpacking.

### Fixed
- **A failed relaunch no longer tears the service down.** The service shuts
  down only after the new process really started; when the spawn fails it
  keeps running and reports a failed install, and the interpreter is looked
  up in the installed copy and then in the parent dev clone.

## 0.4.3 — 2026-11-23

### Added
- **The update check is always in sight.** The settings window header now
  carries the running version and a Check for updates button next to it on
  every page. A click checks GitHub and answers right in the window: the
  latest version, an available release with Download and Later, a network
  failure, or a request to wait while a recording finishes. Download runs
  the same installation command as before and restarts the app; Later
  dismisses the offer until the next check. The duplicate check button at
  the bottom of the System page is gone; the update toggles stay.

## 0.4.2 — 2026-11-23

### Fixed
- **Rare 10–20 s waits before the final text are gone.** A live partial now
  reads only the last 10 seconds of the take and decodes greedily at one
  temperature, so it no longer holds the engine lock for the whole growing
  buffer. The final dictation pass keeps beam 5 and a two-step temperature
  fallback (0.0 and 0.2) instead of six steps. Each partial's duration is
  logged: above 2 s as `Slow partial`, otherwise at debug level. File mode
  keeps its full temperature fallback and its behavior is unchanged.

## 0.4.1 — 2026-11-23

### Added
- **System audio capture.** A take can now record what the computer plays
  instead of the microphone: set `"audio_source": "system"` in `config.json`
  and press the hotkey; the same hotkey stops the take. The default output
  is captured through its WASAPI loopback (works on Bluetooth, USB and HDMI
  outputs, not only the on-board jack), so a YouTube video or a film can be
  transcribed. Silence auto-stop and live partial text are off in this mode;
  `max_record_s` still applies. An unavailable loopback shows a clear
  notification instead of pasting an empty result. The Sound source switch
  (Microphone / System audio) now lives on the Settings window's Record tab;
  it ships in this same release.
- **A PortAudio device refresh.** When Windows re-creates an audio endpoint
  while the service runs, the cached device table is re-read once and the
  same capture plans are retried, so a 48 kHz WASAPI take comes back without
  restarting the application. The log line now names the host API
  (`api=Windows WASAPI`) next to the device and rate.

### Changed
- **The speech band is kept whole.** The resampler from a device rate to
  16 kHz now uses 127 taps with a Kaiser beta of 10 instead of 25/8: flat to
  6 kHz, aliases from 9 kHz suppressed (previously a 9 kHz tone folded to
  7 kHz at -13.7 dB).
- **The VAD keeps the beginning of a phrase.** `speech_pad_ms` grows from 30
  to 400 and `min_silence_duration_ms` from 250 to 1000, so the first word
  and the closing punctuation survive in both dictation and file mode.
- `soundcard>=0.4.6` joins the runtime dependencies for the loopback.

## 0.4.0 — 2026-11-22

### Added
- **Batch folder transcription and a visible progress strip.** Pick
  **Transcribe folder…** in the tray or call `--transcribe-folder <dir>`:
  every supported audio and video file in the folder (top level by default,
  `--recursive` for subfolders) joins the same queue and the same engine, one
  file at a time. Outputs land beside each source or mirror the tree under
  `--out`; an existing output is skipped unless `--force`; two sources with
  the same stem make the second one a recorded failure, not a stopped batch.
  One bad file is recorded and the rest continue. Dictation keeps priority: a
  batch pauses between segments and resumes on the same file. Cancel stops
  the current file and the queue. The CLI answers with a JSON summary and
  exit codes 0/1/2/3/4 (4 = nothing to transcribe). The HUD now shows a real
  bottom strip for a single file and a batch: a moving bar, the current file
  name, its percent, the batch counter `N/M — P%`, distinct
  queued/running/paused/done/error states, the error of one file while the
  batch goes on, and a result that hides 8 s after the end. The dictation
  overlay, the hotkey and drag behaviour stay untouched.

### Changed
- **Batched file transcription for long files.** File jobs at or above 60 s
  now run through `faster_whisper.BatchedInferencePipeline` on the same loaded
  model, in 600 s blocks, with the same language, VAD, prompt and
  anti-hallucination settings; short files and dictation stay sequential. If
  the batched path is unavailable or fails mid-file, the job falls back to the
  sequential path without duplicates. New keys: `file_batched` (default true)
  and `file_batch_size` (default 8, kept within 1-16). Measured on
  `long_ru.mp4` (660 s, large-v3-turbo, cuda float16): 5.21x against the
  sequential run (103.6 s vs 19.9 s median), CER 0.79 % on `long_ru.mp4` and
  0 % on `test_ru.wav`, peak RSS 1.00x.

### Fixed
- **No popup when the target window changed.** A hotkey dictation that
  finishes in another window no longer asks where to paste: with
  `confirm_focus_change` on, the text waits in the panel draft with its
  insert, correct, copy, dictionary and cancel actions; with it off, the
  text goes to the clipboard as before. The Tk focus dialog, its
  `ask_focus_change` helper and the `focus.*` locale keys are gone.

## 0.3.0 — 2026-11-22

### Added
- **File transcription.** The same faster-whisper engine now takes audio and
  video files (mp4, mkv, mov, webm, mp3, wav, m4a) in addition to the
  microphone. PyAV decodes the file to 16 kHz mono float32 with a decoder of
  our own: faster-whisper 1.2.1 passes `metadata_errors` to `av.open`, which
  av 19.0.2 rejects, so a path handed to `transcribe` raised TypeError.
  Recognition yields segments with start and end times; no ffmpeg binary is
  needed.
- **TXT, JSON, SRT and VTT output.** Results land beside the source file by
  default (`--out` overrides); an existing file is not overwritten without
  `--force`. JSON carries the text, language, model, duration and the
  segments with their timestamps.
- **File job queue with dictation priority.** One queue, one worker, one
  running job. A dictation that starts while a file is being transcribed
  pauses the job between segments and resumes it when the service is idle
  again; cancel is cooperative; progress is measured against the file
  duration.
- **Tray action and HUD progress.** Tray -> **Transcribe file...** opens a
  file dialog and queues the job; the panel shows its own line
  ("Transcribing: 42%") without touching the dictation number; a finished
  job offers **Open result folder**.
- **OpenAI-compatible endpoint.** `POST /v1/audio/transcriptions` takes
  multipart form data (`file`, `model`, `language`, `response_format`,
  `prompt`, `temperature`), accepts `Authorization: Bearer` and
  `X-VibeForma-Token`, answers `json`, `text`, `srt`, `vtt` and
  `verbose_json`, and refuses a file longer than `file_max_sync_s` (900 s)
  with 413 pointing at `/jobs`.
- **Job API.** `POST /jobs`, `GET /jobs/<id>`, `DELETE /jobs/<id>` and
  `GET /jobs/<id>/result?format=...` for long files, plus
  `GET /capabilities` and new `file_mode`, `jobs_running` and `jobs_queued`
  fields in `GET /health`.
- **CLI over a named pipe.** `--transcribe` asks the running service over
  the current user's pipe (`VibeForma-<user>` under the pipe namespace,
  owner-only DACL), needs no port and no token, prints progress to stderr and
  the result to stdout (`--json` for a machine-readable answer). Exit codes:
  0 success, 1 service or job error, 2 bad arguments, 3 cancelled. `--help`
  prints the whole contract.
- **Launcher and discovery.** `run.cmd` without arguments still starts the
  panel with `pythonw.exe`; with arguments it forwards them to
  `python.exe main.py`, so stdout, stderr and the exit code reach the caller.
  `location.json` is now schema 2 and publishes `launcher`, `python`,
  `entry`, `pipe`, `formats`, `languages` and the limits, written atomically
  and without secrets.
- **Config and limits.** `file_roots`, `file_max_mb`, `file_max_s`,
  `file_max_sync_s`, `job_keep_s` and `file_word_timestamps` join the
  defaults; an existing `config.json` keeps working.

## 0.2.4 — 2026-11-22

### Added
- **Recognition overlay.** While the model turns a phrase into text, the
  panel dims and a fast percentage runs across it, with a thin ice-to-cyan
  line underneath. The counter rises quickly, holds below 100% while the
  model is still busy, and jumps to 100% only when the text is ready (the
  recognizer reports no measured percentage, so the number is an animated
  estimate). The overlay fades out as the recognized text appears.
- **Settings history is a table.** The History section fills the right side
  with a sortable-by-time table (when / chars / application / text), a phrase
  search, and three filters — application, date (today, 7 or 30 days) and
  direction (inserted or copied). Rows are selected (several at once), Delete
  works via the button or key, and the actions sit above the table. The two
  history switches stay below it; the settings window now sits above the
  monitoring panel so the table is not covered.
- **Panel dictation.** The monitoring panel has a microphone button again.
  Press it and the recognized text stays in the panel, with **Edit**,
  **Copy**, **Insert at the cursor** and **Cancel** buttons underneath — no
  separate popup. Insert pastes into the window you focus after the draft
  appears and keeps a maximized window maximized; if no window is picked,
  the text goes to the clipboard. The **‹ ›** arrows in the panel header
  flip through saved dictations when history is on, and the same action
  row works on the shown record. The panel keeps its top edge and grows
  downward, so the header and the arrows stay in place. With the panel
  focused, the hotkey starts a new panel draft and clears the history view.
  The hotkey flow from other windows is unchanged.
- **Snippets.** A spoken trigger becomes prepared text after recognition:
  the match is literal, case-insensitive and respects word boundaries, the
  longest trigger wins, one pass runs, and the text keeps its line breaks.
  The section has add / update / remove, import and export as JSON, and a
  preview of what a sample phrase becomes; up to 500 pairs.
- **Local statistics.** A Statistics section counts dictations, words,
  characters and seconds per day in `stats.json` beside `config.json`.
  Only numbers are stored, never the text; the table can be reset.
- **Dictionary learning.** The panel draft, history and the dictionary
  dialog turn a correction into a pair "as heard -> as needed"; when the
  same replacement repeats, the settings window offers it as a suggestion.
  A pair enters the dictionary only after an explicit confirmation, and the
  small corrections file stays on this computer.
- **Text rules (off by default).** A spoken correction ("no, more precisely",
  "I mean") replaces the abandoned phrase from its start with the new
  wording, or drops it when nothing follows; spoken enumerators become a
  numbered list. Deterministic and offline, no model.
- **Hold to talk.** The hotkey can work as a hold key: press, speak, release
  to stop, instead of the press-to-start / press-to-stop toggle.
- **Esc cancels a dictation.** The listener exists only while a dictation is
  in flight, so Esc keeps its ordinary meaning everywhere else.
- **External hook (off by default).** The recognized text goes to the user's
  command on stdin, UTF-8, without a shell; the timeout is kept inside
  1-30 s. A failure or a timeout is a status line in the log, never the text,
  and it never breaks the insertion.
- **Command line.** `--toggle`, `--cancel` and `--status`: the last one reads
  `status.json`, which the service writes on every state change, and refuses
  an answer older than 60 s.
- **A local API for forms.** `GET /health` says whether the service is up,
  which model is loaded and whether it is idle; `POST /transcribe` takes a
  16 kHz mono WAV (up to 10 MB, one call per second, idle only) and deletes
  the temporary file. The settings switch starts and stops the listener live,
  and the key is minted and saved on the first start, so a hand-written
  config with the switch on still works.
- **Automatic API port and discovery.** If the configured port (8787 by
  default) is busy, the service binds a free loopback port instead of
  silently losing the API. It publishes
  `%LOCALAPPDATA%\VibeForma\location.json` with the config path, the actual
  port and the API state, so a form reads the endpoint from one file and
  never hardcodes a port. `/health` now identifies the app and names the
  actual port.
- **Hotkeys tab.** Settings lists every key the app listens for: the
  dictation hotkey (toggle or hold-to-talk), Esc cancelling a phrase,
  Ctrl+Enter inserting a panel draft, the paste keys, and the settings and
  history shortcuts. Only the dictation hotkey is editable; the rest are
  fixed and shown for reference.

### Fixed
- **Microphones that only open at 44.1 or 48 kHz.** Capture follows the
  device: WASAPI at the device rate in mono, then in stereo when the device
  offers it, the legacy host API at its native rate, and 16 kHz only as the
  last resort. A rate above 16 kHz is converted by a windowed-sinc resampler
  with a real stopband instead of linear interpolation, so speech no longer
  picks up aliasing from the rate conversion.
- **A failed device open is retried once** through the clean path, and the
  log names the one-line reason for the fallback.

## 0.2.3 — 2026-11-20

### Removed
- **The microphone icon is gone from the monitoring panel.** It could not
  place the cursor or insert text reliably; dictation is started with the
  hotkey only.

### Added
- **Update button on the panel.** When a newer release is found, a blue
  **Update** button appears on the monitoring panel next to the close icon.
  It installs the release through the same GitHub bootstrap as the README
  and restarts the service.
- **Updating section in the README (en/ru).** The manual command is spelled
  out, including `-Target` for an install outside the default folder.

### Changed
- **The repository is English-only outside localization resources.** Code,
  comments, logs, the changelog and commit messages are English; Cyrillic is
  allowed only in `app/locales/*.json`, the installer's Russian texts, the
  ru/uk ASR prompts and `README.ru.md`. A test (`tests/test_english_only.py`)
  enforces the rule.
- **Updates are the plain GitHub installation.** The Update button and the
  automatic check use the same command as the README: the release zip from
  GitHub, no extra secrets and no extra install step.

### Removed
- **The separate Windows setup and its build tooling are gone.** The release
  is a plain zip with `SHA256SUMS`; the release workflow needs no secrets,
  and the packed-setup docs were removed with it.

## 0.2.2 — 2026-11-20

### Changed
- **Settings got a redesign.** A sidebar with six sections replaces the tab
  strip: every section has a heading and a one-line description, rows are
  grouped with hairlines, boolean options are switch-style toggles, and the
  footer with Save / Cancel stays pinned at the bottom. `Ctrl+F` focuses the
  search, `Ctrl+S` saves, `Esc` closes.
- **Rounded, dark window chrome.** The settings window and the model-switch
  dialog use the Windows 11 rounded corners and a dark title bar; the
  borderless monitoring panel is clipped to rounded corners as well.

## 0.2.1 — 2026-11-20

### Fixed
- **Shortcuts launch the app directly.** Desktop, Start menu and autostart
  shortcuts now run `pythonw.exe main.py` instead of `run.cmd`, so a broken
  `.cmd` association no longer blocks the launch. The installer starts the
  service the same way.
- **The monitoring panel can be brought back.** The tray has **Show panel**,
  and launching the app again asks the running instance to show the panel
  instead of exiting silently.
- **The settings footer is always visible.** Save / Cancel are packed before
  the expanding notebook: at 150% scaling the tab content scrolls and the
  buttons are not cut off.

### Added
- **Install folder choice.** The installer asks where to install (default
  `%LOCALAPPDATA%\VibeForma`); `--target` and the bootstrap `-Target` set the
  folder without the question, for example `D:\VibeForma`. The README (en/ru)
  describes the choice.

## 0.2.0 — 2026-11-20

### Added
- **A Quick start at the top of the README.** README (en/ru) now opens with
  the one-command PowerShell install (`irm .../install.ps1 | iex`), what it
  does, where it installs, and the bootstrap options `-Model`, `-Cpu`,
  `-DryRun`, `-NoRun`, plus an honest note that only released builds are
  installed, and the source installer flags from `install.py --help`.
- **A support report and a troubleshooting guide.** `--doctor`
  (`run.cmd --doctor`, tray → **Diagnostics…**,
  Settings → **System** → **Diagnostics…**) checks Python and the service
  dependencies, the model on disk, the CUDA/CPU profile, the microphone,
  the hotkey, update settings, disk space and the privacy switches, prints
  `OK` / `WARN` / `FAIL` and exits 0 or 1. The Tk window has a
  **Copy report** button. The report is read-only and never contains
  dictated text, window titles, `hotwords`, the dictionary, the history or
  the HTTP token. `docs/TROUBLESHOOTING.md` covers the microphone, a busy
  HTTP port, disk space, model loading, the hotkey, insertion and updates;
  README (en/ru) links it and answers "where the audio goes" and
  "what stays on disk".
- **Release notes at update time.** The update notification carries a short
  plain-text excerpt of the release notes, and the tray adds **What's new
  in …** that opens the https release page only. A release without a page
  keeps the plain notification.
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
- **Consent-based updates.** A release ships `SHA256SUMS` next to the zip;
  `auto_update` defaults to `notify`: the tray offers Update / Later, and
  only an explicit opt-in installs without asking. The `updating` state
  blocks the hotkey and recording.
- `tools/build_release.py` and `.github/workflows/release.yml`: build the zip
  with `test_ru.wav` and write `SHA256SUMS`.
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

### Changed
- **The one-command install from GitHub is the main path.** The README says
  so explicitly: the bootstrap command installs the latest release into the
  user's Python environment.
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

### Fixed
- The settings window opens at one stable size and place. Its geometry is
  computed once from the tallest tab, clamped to the screen (short screens
  scroll instead of cutting fields off), centred on the monitor and given a
  minimum size; every tab body scrolls with a dark scrollbar that appears
  only when needed, and the Save/Cancel footer stays visible. Spacing scales
  with the display DPI, and the footer is separated from the content.
- The monitoring panel no longer stretches during dictation. Its height is
  computed from the base on every change (it used to be recomputed from the
  previous, already grown height, so the panel ratcheted taller and crawled
  past the screen edge); it now follows the text the widget actually shows,
  keeps its bottom edge in place and returns to the base size when the text
  is short.
- A fresh default install no longer refuses itself: the installer wrote
  `install.log` into `%LOCALAPPDATA%\VibeForma\logs` before the target
  preflight, so the empty-folder check saw a non-empty target. The log
  opens after the preflight and buffers its first lines.
- `--selftest` and `--doctor` find `test_ru.wav` in a fresh git checkout
  (the file sits at the repository root, not in `app/`); an installed copy
  is unchanged.
- `install.py --dry-run` no longer creates `install.log` in the default
  data folder: the plan is printed only, and a log is written only when
  `--log` names a file (matching "execute nothing").

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
- The status line shows the actual hotkey ("Ready — Ctrl+Alt+Space")
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
  language in its own script (English, Russian, Ukrainian, German,
  Japanese…). Codes are still stored in config.json.

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
