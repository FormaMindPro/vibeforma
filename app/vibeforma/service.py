"""The dictation service: recording, recognition, insertion into the field."""
from __future__ import annotations

import contextlib
import gc
import os
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import dictionary
import errors
import hallucination
import history
import snippets
import stats
import textclean
import textrules
import transcript
from config import DATA_DIR, VERSION, find_input_device, match_app_profile

from . import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_UPDATING,
)
from .actions import HistoryActions
from .settings_ops import SettingsOps
from .status import write_status
from .update_ops import UpdateActions
from .util import beep

CONTINUOUS_QUEUE_CHUNKS = 8   # chunks waiting for the background final pass
CONTEXT_TAIL_CHARS = 160      # one context source in hotwords, cut at a word
_SEAM_WORDS = 12              # a repeated run at a seam is never longer
_SENTENCE_END = frozenset(".!?\u2026")
_WORD_EDGES = "\"'()[]{}<>,.;:!?\u2014\u2013-"
# Live subtitles (F9): the plate is the last one or two phrases of a system
# take, never a transcript. It is bounded, and a text-only partial has no
# engine readings, so only a short known stop phrase or a loop is rejected.
SUBTITLE_PHRASES = 2
SUBTITLE_MAX_CHARS = 400
SUBTITLE_STOP_MAX_WORDS = 12


def _bare_word(word: str) -> str:
    """A seam word without edge punctuation or case, for comparison."""
    return word.strip(_WORD_EDGES).casefold()


def _edge_punctuation(word: str) -> str:
    """The punctuation this word ends with ('' when it has none)."""
    return word[len(word.rstrip(_WORD_EDGES)):]


def _keep_punctuation(kept: str, dropped: str) -> str:
    """Two copies of one seam word: keep the stronger trailing punctuation."""
    kept_tail = _edge_punctuation(kept)
    dropped_tail = _edge_punctuation(dropped)
    if not dropped_tail or kept_tail[-1:] in _SENTENCE_END:
        return kept
    if not kept_tail:
        return kept + dropped_tail
    return kept[:len(kept) - len(kept_tail)] + dropped_tail


def join_chunks(parts: list[str]) -> str:
    """Join recognized pieces into one text without repeating seam words.

    A continuous take is recognized piecewise; every seam may repeat words
    (the overlap carried across a hard cut, or the engine repeating the
    boundary). The longest shared run is dropped from the next piece, and its
    punctuation is kept on the word that stays, so a sentence end is never
    lost. A trailing piece that is only a prefix of the next piece's first
    word is the word split by a hard boundary: the complete word follows.
    """
    pieces = [part.strip() for part in parts if part and part.strip()]
    if not pieces:
        return ""
    if len(pieces) == 1:
        return pieces[0]
    words: list[str] = []
    for piece in pieces:
        piece_words = piece.split()
        if words and piece_words:
            last = _bare_word(words[-1])
            first = _bare_word(piece_words[0])
            if (len(last) >= 4 and len(last) < len(first)
                    and first.startswith(last)
                    and _edge_punctuation(words[-1])[-1:] not in _SENTENCE_END):
                words.pop()
        limit = min(len(words), len(piece_words), _SEAM_WORDS)
        for size in range(limit, 0, -1):
            left = [_bare_word(word) for word in words[-size:]]
            right = [_bare_word(word) for word in piece_words[:size]]
            if left == right:
                words[-1] = _keep_punctuation(words[-1], piece_words[size - 1])
                piece_words = piece_words[size:]
                break
        words.extend(piece_words)
    return " ".join(words)


def _split_phrases(text: str) -> list[str]:
    """One cumulative partial -> phrases: sentence ends and line breaks cut.

    A partial re-recognizes the last PARTIAL_WINDOW_S on every tick, so the
    plate splits it into sentences and keeps only the end. A closing quote or
    bracket that lands in its own piece stays with the phrase it closes.
    """
    pieces: list[str] = []
    current: list[str] = []
    for ch in str(text or ""):
        current.append(ch)
        if ch in _SENTENCE_END or ch == "\n":
            phrase = "".join(current).strip()
            if phrase:
                pieces.append(phrase)
            current = []
    tail = "".join(current).strip()
    if tail:
        pieces.append(tail)
    merged: list[str] = []
    for phrase in pieces:
        if merged and not any(ch.isalnum() for ch in phrase):
            merged[-1] += phrase
        else:
            merged.append(phrase)
    return merged


def _subtitle_rejection(phrase: str) -> str | None:
    """Why a text-only partial must not reach the plate, or None.

    A partial carries no ``no_speech_prob`` and no compression ratio, so the
    final-path guard cannot judge it. The honest limit is two text-only
    families: a repetition loop, and a known stop phrase only while the
    phrase is short — the same words inside real speech are kept.
    """
    if not any(ch.isalnum() for ch in phrase):
        return "no words"
    if hallucination.is_loop(phrase):
        return "repetition loop"
    if (len(phrase.split()) <= SUBTITLE_STOP_MAX_WORDS
            and hallucination.stop_phrase(phrase)):
        return "stop phrase"
    return None


def _tail_words(text: str, limit: int) -> str:
    """The end of a too-long phrase, cut at a word edge when one is near."""
    tail = text[-limit:]
    space = tail.find(" ")
    if 0 <= space < limit // 4:
        tail = tail[space + 1:]
    return tail.strip()


def _subtitle_plate(text: str) -> list[str]:
    """The last phrases of one cumulative partial: bounded and deduplicated.

    The partial slides across the take, so the same sentence is recognized
    again and again at the window's edge; identical neighbours collapse. The
    total length is capped — the HUD is a glance, not a transcript.
    """
    kept: list[str] = []
    for phrase in reversed(_split_phrases(text)):
        if _subtitle_rejection(phrase) is not None:
            continue
        if (kept
                and hallucination.normalize(phrase)
                == hallucination.normalize(kept[0])):
            continue
        kept.insert(0, phrase)
        if len(kept) == SUBTITLE_PHRASES:
            break
    total = sum(len(phrase) for phrase in kept) + max(0, len(kept) - 1)
    while len(kept) > 1 and total > SUBTITLE_MAX_CHARS:
        total -= len(kept.pop(0)) + 1
    if kept and len(kept[0]) > SUBTITLE_MAX_CHARS:
        kept[0] = _tail_words(kept[0], SUBTITLE_MAX_CHARS)
    return kept


def _shift_segments(rows: list[transcript.Segment],
                    offset: float) -> list[transcript.Segment]:
    """Chunk segments on the take's timeline; the readings are kept."""
    shifted: list[transcript.Segment] = []
    for row in rows:
        words = None
        if row.words:
            words = [replace(word, start=word.start + offset,
                             end=word.end + offset) for word in row.words]
        shifted.append(replace(row, start=row.start + offset,
                               end=row.end + offset, words=words))
    return shifted


def _context_tail(text: str, limit: int = CONTEXT_TAIL_CHARS) -> str:
    """The last words of a context source, on one line and bounded.

    A previous phrase carries the names and the style of the take; only its
    ending goes to the prompt, so a long sentence never spends the hotwords
    budget whole. The cut lands on a word boundary when there is one.
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    tail = flat[-limit:]
    cut = tail.find(" ")
    if cut >= 0:
        tail = tail[cut + 1:]
    return tail.strip()


def _join_hotwords(*parts: Any) -> str:
    """Comma-separated hotword terms from several sources, without repeats.

    The user's own hotwords and the take's context are one set for the
    engine: a term already present is never added twice, whatever its case.
    The profile's terms join the same set later, through the dictionary
    ``hotwords`` merge in ``asr.py``, so the prompt carries every term once.
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for chunk in str(part or "").split(","):
            term = chunk.strip()
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                out.append(term)
    return ", ".join(out)


class Service(HistoryActions, SettingsOps, UpdateActions):
    def __init__(self, cfg: dict[str, Any], log: Any, *,
                 status_file: str | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.state = STATE_LOADING
        self._state_lock = threading.Lock()
        # The CLI reads this file; tests and embedders leave it unset.
        self.status_file = status_file
        self.tray = None
        self.hotkey = None
        self.cancel_watcher = None
        self.asr = None
        self.vad = None
        self.recorder = None
        self.http_server = None
        self.file_jobs = None
        self.pipe: Any = None
        self._last_file_outputs: list[str] = []
        self._running = True
        self._partial_busy = False
        self._cancel_evt = threading.Event()
        # Continuous dictation: finalized chunk texts, their segments on the
        # take's timeline, and the one worker that recognizes the queue.
        self._chunk_q: queue.Queue[Any] = queue.Queue(
            maxsize=CONTINUOUS_QUEUE_CHUNKS)
        self._chunk_texts: list[str] = []
        self._chunk_segments: list[transcript.Segment] = []
        self._chunk_offset = 0.0
        self._chunk_worker: threading.Thread | None = None
        self._chunk_lock = threading.Lock()
        self._update_busy = False
        self._update_failed = False
        self._test_mode = False
        self._panel_mode = False
        self._draft_busy = False
        # The draft's wider pass (card-247): one retry at a time, and the
        # text held before it so the person can roll back. The WAV of the
        # draft's take lives in ``_draft_wav`` beside the text it produced.
        self._retry_busy = False
        self._draft_prev_text = ""
        self._draft_prev_uncertain: list[str] = []
        self._draft_prev_offer = False
        self._history_cursor = None
        self._history_saved = ""
        self._history_saved_uncertain: list[str] = []
        self._history_total = 0
        self._history_saved_wav: bytes | None = None
        self._draft_wav: bytes | None = None
        self.pending_update = None
        self.update_check_state = "idle"
        self.update_check_tag = ""
        # A voice action (card-250) may ask for another model; the switch
        # waits for the idle state set_model needs.
        self._pending_model = ""
        self.panel_request = threading.Event()
        self.history = history.History(history.history_path(DATA_DIR))
        if "history_audio" in self.cfg and not self.cfg.get("history_audio"):
            # Off means off across restarts too: no sound from an earlier run
            # outlives the option. A partial config (an embedder, a test) does
            # not name the option, so it never deletes anything.
            self.history.drop_audio()
        self.stats = stats.Stats(stats.stats_path(DATA_DIR))
        self._record_started = 0.0
        self._record_seconds = 0.0
        self._last_insert = None
        self._target_title = ""
        self._target_hwnd = None
        # Dictation context (F11): the previous phrase and the selected text
        # of this take, in memory only. Off by default, so nothing is read.
        self._last_phrase = ""
        self._context_hotwords = ""
        # By-app profiles (card-251): the take's target address fixes the
        # profile once; the later foreground window never re-picks it.
        self._app_profile_ready = False
        self._app_profile_hwnd: Any = None
        self._app_profile_cached: dict[str, Any] | None = None
        # shared live view for the HUD (written by worker threads, read by GUI)
        self.live: dict[str, Any] = {
            "state": STATE_LOADING,
            "level": 0.0,
            "vad": 0.0,
            "partial": "",
            "subtitles": [],
            "draft": "",
            "draft_rev": 0,
            "draft_offer": False,
            "draft_rollback": False,
            "draft_audio": False,
            "language_hint": None,
            "last": "",
            "last_ts": 0.0,
            "device": "",
            "exit": False,
            "privacy": bool(self.cfg.get("hud_privacy", False)),
            "open_onboarding": False,
            "open_settings": False,
            "settings_tab": None,
            "test_text": "",
            "test_state": "",
            "file_job": {},
            "file_batch": {},
        }
        self.publish_status()

    # -- wiring --------------------------------------------------------
    def attach_tray(self, tray: Any) -> None:
        self.tray = tray

    def attach_hotkey(self, hotkey: Any) -> None:
        self.hotkey = hotkey

    def attach_cancel_watcher(self, watcher: Any) -> None:
        self.cancel_watcher = watcher

    def request_panel(self) -> None:
        """Ask the HUD to show itself; safe from any thread."""
        self.panel_request.set()

    # -- state ---------------------------------------------------------
    def publish_status(self, state: str | None = None) -> None:
        """Write status.json for the CLI; a failed write never breaks us."""
        status_file = getattr(self, "status_file", None)
        if not status_file:
            return
        if not write_status(status_file, state or self.state, VERSION):
            self.log.debug("status.json not written: %s", status_file)

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self.state = state
        self.live["state"] = state
        self.publish_status(state)
        self._sync_cancel_watcher(state)
        jobs = getattr(self, "file_jobs", None)
        if jobs is not None:
            # A dictation outranks any file job: between segments the job
            # pauses and resumes once the service is idle again.
            jobs.dictation_active(
                state in (STATE_RECORDING, STATE_PROCESSING))
        if self.tray:
            self.tray.set_state(state)
        self._maybe_apply_pending_update()
        self._maybe_apply_pending_model()

    def _sync_cancel_watcher(self, state: str) -> None:
        """The global Esc binding lives only while a dictation is in flight.

        Recognition still takes Esc: the user can drop a result already in
        the worker. At rest the listener is gone, so Esc stays with the
        foreground application.
        """
        watcher = self.cancel_watcher
        if watcher is None:
            return
        try:
            if state in (STATE_RECORDING, STATE_PROCESSING):
                watcher.start()
            else:
                watcher.stop()
        except Exception as e:  # noqa: BLE001 - a failed listener must not break dictation
            self.log.debug("Esc watcher: %s", e)

    # -- hotkey ---------------------------------------------------------
    def on_hotkey(self) -> None:
        """Toggle mode only: a single click opens or closes a phrase.

        In hold-to-talk the app wires HoldHotkey to on_hotkey_press and
        on_hotkey_release instead; the CLI toggle still lands here.
        """
        st = self.state
        if st == STATE_IDLE:
            # If the panel owns the foreground, a new phrase belongs to the
            # panel: it clears the history/draft and shows the new text there.
            import insert as insert_mod
            self._panel_mode = bool(insert_mod.is_own_window(
                insert_mod.get_foreground_hwnd()))
            self.start_recording()
        elif st == STATE_RECORDING:
            self.stop_recording("manual")
        elif st == STATE_PROCESSING:
            self.log.info("Hotkey during recognition — ignored")
        elif st == STATE_PAUSED:
            self.log.info("Service is paused — hotkey ignored")
        elif st == STATE_UPDATING:
            self.log.info("Update in progress — hotkey ignored")
        else:  # loading / error
            self.log.info("Model is not ready yet — hotkey ignored")

    def on_hotkey_press(self) -> None:
        """Hold-to-talk: the combo went down, start a phrase from rest.

        Auto-repeat never reaches this method: ComboTracker emits the down
        event once per hold, so a held combo cannot start a second phrase.
        """
        if self.state != STATE_IDLE:
            self.log.info("Hold hotkey press ignored: %s", self.state)
            return
        import insert as insert_mod
        self._panel_mode = bool(insert_mod.is_own_window(
            insert_mod.get_foreground_hwnd()))
        self.start_recording()

    def on_hotkey_release(self) -> None:
        """Hold-to-talk: the combo came up, finish the phrase in flight."""
        if self.state != STATE_RECORDING:
            return
        self.stop_recording("hold")

    # -- panel dictation -------------------------------------------------
    def on_panel_mic(self) -> None:
        """Mic button on the HUD: record, then show the text in the panel.

        A click on the borderless panel leaves no text field to paste into,
        so the recognized text waits in the panel until the user reviews
        it and picks Insert, Edit or Cancel.
        """
        st = self.state
        if st == STATE_IDLE and not self._draft_busy:
            self._panel_mode = True
            self.start_recording()
        elif st == STATE_RECORDING:
            self.stop_recording("panel")
        elif st in (STATE_PROCESSING, STATE_UPDATING):
            self.log.info("Mic button ignored while busy: %s", st)
        else:
            self.log.info("Mic button ignored — the model is not ready: %s", st)

    # -- recording -------------------------------------------------------
    def start_recording(self) -> None:
        self._forget_draft()
        if self.cfg.get("beep_on"):
            beep(1200, 70)
        dev_idx = find_input_device(self.cfg["input_device"])
        try:
            import sounddevice as sd
            dev_name = str(sd.query_devices(dev_idx)["name"])[:40]
        except Exception as e:  # noqa: BLE001 - PortAudio has no stable base exception
            self.log.debug("Input device name: %s", e)
            dev_name = str(self.cfg.get("input_device", ""))
        self.live["device"] = dev_name
        self.live["partial"] = ""
        self.live["subtitles"] = []
        self.live["level"] = 0.0
        # A new take starts with no language note: the previous one is not
        # about the audio being recorded now.
        self.live["language_hint"] = None
        self._partial_busy = False
        self._cancel_evt.clear()
        self._reset_chunks()
        self._app_profile_ready = False   # a new take matches its own target
        if self._panel_mode:
            # The panel is the foreground window; there is no field to aim at.
            self._target_hwnd = None
            self._target_title = ""
        else:
            import insert as insert_mod
            self._target_hwnd = insert_mod.get_foreground_hwnd()
            title_wanted = (self.cfg.get("log_window_titles", False)
                            or self.cfg.get("history_app", False))
            self._target_title = (insert_mod.foreground_window()
                                  if title_wanted else "")
            if self.cfg.get("log_window_titles", False):
                self.log.info("Paste target window: %s (hwnd=%s)",
                              self._target_title, self._target_hwnd)
            else:
                self.log.info("Paste target window: hwnd=%s", self._target_hwnd)
        self.recorder = self._make_recorder()
        self._record_started = time.time()
        try:
            self.recorder.start(dev_idx)
        except Exception as e:
            self._record_started = 0.0
            self._record_seconds = 0.0
            source = str(self.cfg.get("audio_source") or "microphone")
            kind = "system" if source == "system" else "mic"
            if kind == "system":
                self.log.exception("System audio unavailable: %s", e)
            else:
                self.log.exception("Microphone unavailable: %s", e)
            if self._test_mode:
                self._finish_mic_test("", "error")
            self._set_state(STATE_IDLE)
            if self.tray:
                self.tray.notify(errors.text(self.cfg, kind, e))
            return
        self._set_state(STATE_RECORDING)
        # The take is already capturing: a slow clipboard read never eats the
        # first word, and the first chunk or partial lands after it anyway.
        self._capture_dictation_context()

    def _capture_dictation_context(self) -> None:
        """Collect this take's hotwords context; option off — nothing is read.

        On, the ending of the previous phrase and the text selected in the
        target window bias the recognition: a name is written the same way
        and the style does not jump. The selection is copied through the
        clipboard by ``insert.read_selected_text`` and put back there; the
        panel has no field to read from. Only the lengths are logged, never
        the words themselves, and nothing is written to ``config.json``.
        """
        self._context_hotwords = ""
        if self._test_mode or not self.cfg.get("dictation_context", False):
            return
        parts = []
        previous = _context_tail(getattr(self, "_last_phrase", ""))
        if previous:
            parts.append(previous)
        if not self._panel_mode:
            import insert as insert_mod
            try:
                selected = _context_tail(
                    insert_mod.read_selected_text(self.log))
            except Exception as e:  # noqa: BLE001 - a context read never breaks a take
                self.log.debug("Dictation context: selection skipped: %s", e)
                selected = ""
            if selected:
                parts.append(selected)
        self._context_hotwords = ", ".join(parts)
        if parts:
            self.log.info("Dictation context: %d source(s), %d chars",
                          len(parts), len(self._context_hotwords))

    def _app_profile(self) -> dict[str, Any] | None:
        """The by-app profile of the window this take was aimed at.

        The address is ``_target_hwnd``, captured when the recording started
        (card-251): the profile follows the window the person spoke into,
        never the window that happens to be active when the text is ready.
        No address (the panel), no profiles or an unreadable window all mean
        the global rules only. Resolved once per address; a new take clears
        the cache.
        """
        hwnd = getattr(self, "_target_hwnd", None)
        # An embedder or a test double may construct the service without
        # __init__: no cache is ready there, the profile is resolved fresh.
        if (getattr(self, "_app_profile_ready", False)
                and hwnd == getattr(self, "_app_profile_hwnd", None)):
            return getattr(self, "_app_profile_cached", None)
        profile = None
        profiles = self.cfg.get("app_profiles")
        if profiles and hwnd:
            import insert as insert_mod
            try:
                process, title = insert_mod.window_identity(hwnd)
                profile = match_app_profile(profiles, process, title)
            except Exception as e:  # noqa: BLE001 - an unreadable window keeps the global rules
                self.log.debug("App profile skipped: %s", e)
                profile = None
        if profile is not None:
            self.log.debug("App profile: %s (process %s)", profile.get("name"),
                           profile.get("process") or "-")
        self._app_profile_ready = True
        self._app_profile_hwnd = hwnd
        self._app_profile_cached = profile
        return profile

    @contextlib.contextmanager
    def _context_engine(self, engine: Any = None) -> Iterator[None]:
        """One recognition call with this take's context in the hotwords.

        ``asr.py`` is untouched (card-248): its ``_hotwords`` reads the
        config dict it was given, so the engine gets a shallow copy carrying
        the merged string for the call and the user's dict is put back after.
        The copy also carries the take's dictionary — the profile's entries
        over the global ones (card-251) — so the recognition prompt is one
        deduplicated set of the user hotwords, the profile terms and the
        context. ``config.json`` never sees either: the shared config object
        is not modified.
        """
        extra = getattr(self, "_context_hotwords", "")
        engine = engine if engine is not None else self.asr
        base = getattr(engine, "cfg", None) if engine is not None else None
        if engine is None or not isinstance(base, dict):
            yield
            return
        profile = self._app_profile()
        profile_entries = profile.get("dictionary") if profile else None
        if not extra and not profile_entries:
            yield
            return
        call = {**base, "hotwords": _join_hotwords(base.get("hotwords"), extra)}
        if profile_entries:
            call["dictionary"] = dictionary.merge(base.get("dictionary"),
                                                  profile_entries)
        engine.cfg = call
        try:
            yield
        finally:
            engine.cfg = base

    def _make_recorder(self) -> Any:
        import audio as audio_mod
        # History with sound (card-256) keeps the whole take: the chunk cuts
        # then leave a copy behind, and the WAV is not only the tail.
        keep_take = bool(self.cfg.get("history", False)
                         and self.cfg.get("history_audio", False))
        return audio_mod.Recorder(self.cfg, self.log, self.vad,
                                  self._on_auto_stop,
                                  on_partial=self._on_partial,
                                  on_chunk=self._on_audio_chunk,
                                  keep_take=keep_take,
                                  live_state=self.live)

    # -- continuous dictation (long speech, one insertion) ----------------
    def _reset_chunks(self) -> None:
        """A new phrase starts with no finalized pieces and an empty queue."""
        with self._chunk_lock:
            self._chunk_texts = []
            self._chunk_segments = []
            self._chunk_offset = 0.0
        self._chunk_q = queue.Queue(maxsize=CONTINUOUS_QUEUE_CHUNKS)
        self._chunk_worker = None

    def _on_audio_chunk(self, chunk: Any) -> bool:
        """One bounded piece of the live take: queue it for the final pass.

        The chunk pass is the final dictation pass (beam 5, word timestamps),
        not the greedy preview: the text that reaches the field is built from
        finals with priority over the live partial. Returns False when the
        queue is full, so the recorder keeps the audio and retries later.
        """
        if self.asr is None or self.state != STATE_RECORDING:
            return False
        try:
            self._chunk_q.put_nowait(chunk)
        except queue.Full:
            self.log.info("Continuous chunk deferred: recognition is behind")
            return False
        if self._chunk_worker is None:
            self._chunk_worker = threading.Thread(
                target=self._chunk_loop, args=(self._chunk_q,), daemon=True)
            self._chunk_worker.start()
        return True

    def _chunk_loop(self, chunks: queue.Queue[Any]) -> None:
        """Recognize this take's chunks in order; a failed piece is skipped."""
        while True:
            chunk = chunks.get()
            chunks.task_done()
            if chunk is None:
                return
            engine = self.asr
            if engine is None:
                continue
            try:
                with self._context_engine(engine):
                    text = engine.transcribe(chunk, verbose=False)
                seconds = len(chunk) / 16000.0
                with self._chunk_lock:
                    if text:
                        self._chunk_texts.append(text)
                        self.live["partial"] = join_chunks(
                            list(self._chunk_texts))
                    rows = list(getattr(engine, "last_segments", None) or [])
                    self._chunk_segments.extend(
                        _shift_segments(rows, self._chunk_offset))
                    self._chunk_offset += seconds
                self.log.debug("Continuous chunk: %.1f s, %d chars",
                               seconds, len(text or ""))
            except Exception as e:  # noqa: BLE001 - one piece must not stop the take
                self.log.warning("Continuous chunk recognition failed: %s", e)
                with self._chunk_lock:
                    self._chunk_offset += len(chunk) / 16000.0
            finally:
                rec = self.recorder
                if rec is not None:
                    rec.chunk_ready()

    def _finish_chunks(self) -> threading.Thread | None:
        """Close the chunk queue; the worker still has to drain its backlog."""
        worker, self._chunk_worker = self._chunk_worker, None
        if worker is None:
            return None
        self._chunk_q.put(None)
        return worker

    def _take_is_system(self, rec: Any = None) -> bool:
        """Whether this take is the system source (live subtitles, no insert).

        The recorder knows its source for the whole take; a test double or an
        embedder without one falls back to the configured source. A
        microphone take is never decided for: everything below branches only
        when the answer is system.
        """
        rec = rec if rec is not None else getattr(self, "recorder", None)
        source = getattr(rec, "_source", None) if rec is not None else None
        if source is None:
            source = self.cfg.get("audio_source")
        return str(source or "microphone") == "system"

    def _publish_subtitles(self, text: str) -> None:
        """Publish the last phrases of a system take for the HUD plate.

        The text never reaches the field: a system take has no dictation
        result, only this view. Rejected phrases are named in the log at
        debug — the limit is text-only and cannot claim the final-path
        guard's confidence readings, so no text is logged either.
        """
        for phrase in _split_phrases(text):
            reason = _subtitle_rejection(phrase)
            if reason is not None:
                self.log.debug("Subtitle dropped: %s (%d chars)",
                               reason, len(phrase))
        self.live["subtitles"] = _subtitle_plate(text)

    def _on_partial(self, audio_snapshot: Any) -> None:
        """Streaming partial: transcribe the in-flight phrase for the HUD."""
        if self._partial_busy or self.state != STATE_RECORDING or self.asr is None:
            return
        self._partial_busy = True

        def _run() -> None:
            try:
                t0 = time.monotonic()
                with self._context_engine():
                    text = self.asr.transcribe(audio_snapshot, verbose=False,
                                               partial=True)
                took = time.monotonic() - t0
                audio_s = len(audio_snapshot) / 16000.0
                if took >= 2.0:
                    self.log.info(
                        "Slow partial: audio %.1f s, recognition %.2f s",
                        audio_s, took)
                else:
                    self.log.debug(
                        "Partial: audio %.1f s, recognition %.2f s",
                        audio_s, took)
                if text and self.state == STATE_RECORDING:
                    if self._take_is_system():
                        # System audio is a live subtitle, never a phrase
                        # for the field: the plate carries the tail, the
                        # partial field stays empty.
                        self._publish_subtitles(text)
                    else:
                        # The finalized chunks come first: a partial is only
                        # the tail of the take and never replaces final text.
                        with self._chunk_lock:
                            finalized = join_chunks(list(self._chunk_texts))
                        self.live["partial"] = join_chunks([finalized, text])
            except Exception as e:  # noqa: BLE001 - a failed partial must not stop the recording
                self.log.debug("Partial transcription: %s", e)
            finally:
                self._partial_busy = False
        threading.Thread(target=_run, daemon=True).start()

    def _on_auto_stop(self, reason: str) -> None:
        if self.state == STATE_RECORDING:
            self.stop_recording(reason)

    def stop_recording(self, reason: str) -> None:
        rec, self.recorder = self.recorder, None
        if rec is None:
            self._set_state(STATE_IDLE)
            return
        system = self._take_is_system(rec)
        buf = rec.stop()
        started = self._record_started
        self._record_started = 0.0
        seconds = max(0.0, time.time() - started) if started else 0.0
        self._record_seconds = seconds
        if self.cfg.get("beep_off"):
            beep(900, 70)
        if system:
            # Live subtitles are a view, never a dictation: the take ends
            # with nothing recognized, inserted, remembered or drafted. The
            # last plate stays until the next take replaces it.
            self.live["partial"] = ""
            self.live["level"] = 0.0
            self._panel_mode = False
            self._context_hotwords = ""
            self._app_profile_ready = False
            self._record_seconds = 0.0
            self.log.info("System audio take stopped: %.1f s, live subtitles "
                          "only, nothing inserted", seconds)
            self._set_state(STATE_IDLE)
            return
        self._set_state(STATE_PROCESSING)
        worker = self._finish_chunks()
        threading.Thread(target=self._transcribe_worker,
                         args=(buf, rec, worker), daemon=True).start()

    def cancel_recording(self) -> None:
        """Esc: drop the in-flight dictation, nothing is inserted or saved."""
        st = self.state
        if st == STATE_RECORDING:
            rec, self.recorder = self.recorder, None
            if rec is not None:
                try:
                    rec.stop()
                except Exception as e:  # noqa: BLE001 - a dying stream must not block the cancel
                    self.log.debug("Cancelled recorder stop: %s", e)
            self._record_started = 0.0
            self._record_seconds = 0.0
            self._panel_mode = False
            self.live["partial"] = ""
            self.live["subtitles"] = []
            self.live["level"] = 0.0
            self._context_hotwords = ""   # a dropped take keeps no context
            # The chunk worker drains its backlog and exits on its own; the
            # finalized text is dropped with the phrase.
            self._finish_chunks()
            self._set_state(STATE_IDLE)
            self._report_cancelled()
            self.log.info("Recording cancelled — buffer dropped")
        elif st == STATE_PROCESSING:
            if self._cancel_evt.is_set():
                return
            self._cancel_evt.set()
            self.live["partial"] = ""
            self.live["subtitles"] = []
            self.live["level"] = 0.0
            self._report_cancelled()
            self.log.info("Recognition cancel requested")
        else:
            self.cancel_draft()

    def _report_cancelled(self) -> None:
        message = self.tr("notify.cancelled_recording")
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)

    def _discard_cancelled(self) -> None:
        """Drop a cancelled recognition: no insertion, history or draft."""
        self._cancel_evt.clear()
        self._panel_mode = False
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self.log.info("Recognition cancelled — result dropped")

    def _transcribe_worker(self, buf: Any, rec: Any,
                           chunk_worker: threading.Thread | None = None) -> None:
        try:
            if chunk_worker is not None:
                # Finalized chunks land before the tail: the text is built in
                # take order and inserted once.
                chunk_worker.join()
            if self._cancel_evt.is_set():
                self._discard_cancelled()
                return
            # An embedder or a test double may construct the service without
            # __init__: no chunk state means no chunks were queued.
            lock = getattr(self, "_chunk_lock", None)
            chunk_texts: list[str] = []
            chunk_rows: list[transcript.Segment] = []
            if lock is not None:
                with lock:
                    chunk_texts = list(self._chunk_texts)
                    chunk_rows = list(self._chunk_segments)
            if not rec.had_speech() and not chunk_texts:
                if self._test_mode:
                    self._finish_mic_test("", "empty")
                    return
                self.log.info("No speech captured — nothing inserted")
                self.set_last(self.tr("notify.no_speech_last"))
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.no_speech"))
                return
            t0 = time.time()
            tail_text = ""
            tail_recognized = False
            if buf is not None:
                with self._context_engine():
                    tail_text = self.asr.transcribe(buf)
                tail_recognized = True
            # The engine's own verdict on the take: a low-confidence phrase is
            # never pasted blind, it waits in the panel draft with its
            # doubtful words marked. An engine without readings (an embedder,
            # a test double) keeps the old paste path. The chunk passes use
            # the same final settings, so their readings join the tail's on
            # one timeline.
            rows = chunk_rows
            if tail_recognized:
                rows = rows + list(getattr(self.asr, "last_segments", None)
                                   or [])
            # What the take sounded like (F12): the pinned language stays the
            # recognition language, the HUD only names the mismatch. A take
            # without a reading clears the previous hint.
            hint = getattr(self.asr, "last_language_hint", None)
            self.live["language_hint"] = (
                dict(hint) if isinstance(hint, dict) else None)
            uncertain = transcript.uncertain_words(rows)
            # A doubtful take is what the wider pass is offered for
            # (card-247): the final pass fell back to a temperature above
            # zero, or the engine's own readings mark the phrase unsure. The
            # offer belongs to the take, not to the reason the draft is shown.
            doubtful = transcript.needs_review(rows) or bool(uncertain)
            # One text from the finalized pieces and the tail; seam words are
            # repeated by the overlap and dropped here, never in the field.
            text = join_chunks([*chunk_texts, tail_text])
            dt = time.time() - t0
            self.log.info("Latency to ready text: %.2f s", dt)
            profile = self._app_profile()
            if profile is not None:
                self.log.info("App profile: %s", profile.get("name"))
            text, hits = dictionary.apply(
                text,
                dictionary.merge(self.cfg.get("dictionary"),
                                 profile.get("dictionary") if profile else None))
            if hits:
                self.log.info("Dictionary: %d replacement(s)", hits)
            text, rule_hits = textrules.apply(text, self.cfg)
            if rule_hits:
                self.log.info("Text rules: %d change(s)", rule_hits)
            if self._cancel_evt.is_set():
                self._discard_cancelled()
                return
            if self._test_mode:
                # Onboarding check: show the text, never insert it.
                self.log.info("Microphone test: %d chars (not inserted)",
                              len(text or ""))
                self._finish_mic_test(text)
                return
            if not text:
                self.log.info("Empty transcription — nothing inserted")
                self.set_last(self.tr("notify.not_recognized"))
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.not_recognized_short"))
                return
            self._count_dictation(text)
            # The take as WAV bytes when the person keeps history with sound;
            # one encode per dictation, shared by the draft and the record.
            wav = self._dictation_wav(buf)
            # A continuous take hands its chunks away as it goes: without the
            # history sound option the recorder keeps no full buffer, and the
            # bytes left are only the tail. The wider pass re-reads the whole
            # take, so it is offered only when the take was kept whole.
            tail_only = bool(chunk_texts) and wav is None
            if doubtful and tail_only:
                self.log.info("Draft retry: continuous take without its full "
                              "buffer — the wider pass is not offered")
            # Run snippets (card-181) here, after the text rules and before
            # the draft and insertion paths, so both receive their result.
            text, snippet_hits = snippets.apply(
                text,
                snippets.merge(self.cfg.get("snippets"),
                               profile.get("snippets") if profile else None))
            if snippet_hits:
                self.log.info("Snippets: %d replacement(s)", snippet_hits)
            # Voice actions (card-250): the whole phrase runs a command, picks
            # another model or calls an external agent. Matching comes after
            # the dictionary and the snippets, so a snippet may be the trigger.
            action = self._match_action(text)
            if action is not None and self._run_action(action, text):
                # The phrase was a command: nothing reaches the text field.
                self.live["partial"] = ""
                self.live["level"] = 0.0
                return
            # Opt-in text cleanup (card-254): spoken numbers and dates become
            # digits, filler words go, sentences start with a capital. It
            # runs after the dictionary, the text rules and the snippets —
            # their output is what the person sees — and after the action
            # match, so a command phrase is compared as it was spoken. The
            # draft and the insertion below both receive the cleaned text.
            text, cleanup_hits = textclean.apply(text, self.cfg)
            if cleanup_hits:
                self.log.info("Text cleanup: %d change(s)", cleanup_hits)
            if not text:
                # The cleanup dropped the whole phrase (fillers only):
                # nothing reaches the field, and the person hears about it.
                self.log.info("Cleanup left no text — nothing inserted")
                self.set_last(self.tr("notify.not_recognized"))
                self.live["partial"] = ""
                self.live["level"] = 0.0
                if self.tray:
                    self.tray.notify(self.tr("notify.not_recognized_short"))
                return
            # A phrase that reaches the field — pasted or drafted — is the
            # context of the next take (F11): the same names, the same style.
            self._last_phrase = text
            # A doubtful draft keeps the take's sound for its wider pass
            # (card-247), even with the history sound option off; a
            # confident phrase never shows the retry, so it pays nothing.
            draft_wav = self._panel_wav(buf, wav, doubtful and not tail_only)
            if self._panel_mode:
                self._panel_mode = False
                self._show_draft(text, wav=draft_wav,
                                 run_hook=action is None,
                                 uncertain=uncertain, offer=doubtful)
                return
            if transcript.needs_review(rows):
                self.log.info("Low confidence — the phrase waits in the "
                              "panel draft")
                self._show_draft(text, wav=draft_wav,
                                 run_hook=action is None,
                                 uncertain=uncertain, offer=doubtful)
                return
            import insert as insert_mod
            target_hwnd = getattr(self, "_target_hwnd", None)
            if (target_hwnd and self.cfg.get("confirm_focus_change", True)
                    and insert_mod.get_foreground_hwnd() != target_hwnd):
                # Dictation started in one window, the text is ready in
                # another: never a silent paste and never a dialog — the text
                # waits in the panel draft.
                self.log.info("Target window changed — draft in the panel")
                self._show_draft(text, wav=draft_wav,
                                 run_hook=action is None,
                                 uncertain=uncertain, offer=doubtful)
                return
            undo_state: dict[str, Any] = {}
            result = insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=target_hwnd,
                paste_method=self.cfg.get("paste_method", "shift_insert"),
                exclude_history=self.cfg.get("clipboard_exclude_history",
                                             True),
                log_title=self.cfg.get("log_window_titles", False),
                undo_state=undo_state)
            self.live["partial"] = ""
            self.live["level"] = 0.0
            if result == insert_mod.RESULT_COPIED:
                self._remember(text, way=history.WAY_COPY, wav=wav)
                self.set_last(self.tr("notify.copied"))
                if self.tray:
                    self.tray.notify(self.tr("notify.copied"))
            elif result == insert_mod.RESULT_CANCELLED:
                self.set_last(self.tr("notify.cancelled"))
            else:
                self._last_insert = {
                    "text": text,
                    "hwnd": getattr(self, "_target_hwnd", None),
                    "ts": time.time(),
                    "prev": undo_state.get("prev"),
                    "prev_known": bool(undo_state.get("prev_known")),
                }
                self._remember(text, wav=wav)
                if action is None:
                    # A failed action already spent this phrase on its own
                    # command: never hand the same command the text again.
                    self._run_hook(text)
                message = self.inserted_message(text)
                self.set_last(message)
                if self.tray:
                    self.tray.notify(message)
        except Exception as e:
            self.log.exception("Recognition error: %s", e)
            beep(400, 200)
            self.set_last(errors.last_line(self.cfg, "recognition", e))
            if self.tray:
                self.tray.notify(errors.text(self.cfg, "recognition", e))
        finally:
            self._cancel_evt.clear()
            # The take is over: its context and its profile never outlive it.
            self._context_hotwords = ""
            self._app_profile_ready = False
            if self._running:
                self._set_state(STATE_IDLE)

    def _count_dictation(self, text: str) -> None:
        """Add one finished phrase to the local counters, once per dictation.

        Runs after the dictionary and the text rules and before the draft
        and insertion branches, so both count once. A mic test, a cancel and
        an empty result return earlier. A broken stats file must never break
        the dictation.
        """
        store = getattr(self, "stats", None)
        if store is None:
            return
        seconds = float(getattr(self, "_record_seconds", 0.0) or 0.0)
        self._record_seconds = 0.0
        try:
            store.record(chars=len(text), words=len(text.split()),
                         seconds=seconds)
        except Exception as e:  # noqa: BLE001 - the counters must not break dictation
            self.log.warning("Stats write failed: %s", e)

    def _dictation_wav(self, buf: Any) -> bytes | None:
        """The take as 16-bit PCM WAV bytes when history sound is on.

        Off (the default) and history off mean ``None``: the encoder never
        runs and nothing reaches the disk. An encoding failure is a line in
        the log, never a lost dictation.
        """
        if not (self.cfg.get("history", False)
                and self.cfg.get("history_audio", False)):
            return None
        if buf is None or not len(buf):
            return None
        try:
            import audio as audio_mod
            data: bytes = audio_mod.wav_bytes(buf)
            return data
        except Exception as e:  # noqa: BLE001 - sound must not break the text
            self.log.warning("History audio skipped: %s", e)
            return None

    def _take_wav(self, buf: Any) -> bytes | None:
        """The take as 16-bit PCM WAV bytes, whatever the history options.

        The draft's wider pass re-reads the sound, so a doubtful draft keeps
        its take even when history sound is off. Nothing reaches the disk
        from here: ``_remember`` gates the history copy on its own, and the
        bytes live in ``_draft_wav`` only while the draft does. An encoding
        failure is a log line, never a lost draft.
        """
        try:
            if buf is None or not len(buf):
                return None
            import audio as audio_mod
            data: bytes = audio_mod.wav_bytes(buf)
            return data
        except Exception as e:  # noqa: BLE001 - the draft must survive a bad buffer
            self.log.warning("Draft audio skipped: %s", e)
            return None

    def _panel_wav(self, buf: Any, wav: bytes | None,
                   needed: bool) -> bytes | None:
        """The WAV a draft keeps: the history copy, or the take for a retry.

        ``needed`` is true for a doubtful take whose full buffer is held;
        only then is the take encoded here. A confident phrase and a
        continuous tail keep what the history option already produced.
        """
        if wav is not None or not needed:
            return wav
        return self._take_wav(buf)

    def _run_hook(self, text: str) -> None:
        """Hand the final text to the user's command in a daemon thread.

        Off while hook_command is empty. Runs after the insertion or the
        draft is already in place, so a slow or failing hook never delays
        what the user sees.
        """
        command = str(self.cfg.get("hook_command") or "").strip()
        if not command:
            return
        import hook as hook_mod
        threading.Thread(
            target=hook_mod.run_hook,
            args=(command, text, self.cfg.get("hook_timeout_s", 5), self.log),
            daemon=True).start()

    def _maybe_apply_pending_model(self) -> None:
        """Switch to the model a voice action asked for, once the service idles.

        ``set_model`` refuses to touch the engine while a dictation is in
        flight, so the action only records the name and the idle transition
        starts the switch.
        """
        # Hand-built services (a test's __new__, an embedder) never ran
        # __init__: a missing field means no request, never a crash.
        name = getattr(self, "_pending_model", "")
        if not name or self.state != STATE_IDLE or self.asr is None:
            return
        self._pending_model = ""
        self.set_model(name)

    def _match_action(self, text: str) -> dict[str, str] | None:
        """The voice action the whole phrase stands for, if any."""
        import hook as hook_mod
        action: dict[str, str] | None = hook_mod.match_action(
            text, self.cfg.get("actions"))
        return action

    def _run_action(self, action: dict[str, str], text: str) -> bool:
        """Run one voice action; True when the phrase was spent on it.

        A ``model`` action only asks: the switch waits for the idle state
        ``set_model`` needs, so the dictation ends first. A command-based
        action runs here, in the worker thread, because its exit code decides
        whether the phrase is still inserted: a failed command leaves the
        words with the person and a status in the log, never a lost
        dictation. The command is the configured hook command unless the
        action names its own.
        """
        kind = action.get("kind") or "command"
        if kind == "model":
            name = action.get("model") or ""
            if not name:
                self.log.warning("Voice action has no model")
                return False
            self._pending_model = name
            self.log.info("Voice action: model %s", name)
            return True
        command = action.get("command") or ""
        if kind == "command" and not command:
            command = str(self.cfg.get("hook_command") or "").strip()
        if not command:
            self.log.warning("Voice action has no command: %s",
                             action.get("trigger", ""))
            return False
        import hook as hook_mod
        ok = hook_mod.run_hook(command, text,
                               self.cfg.get("hook_timeout_s", 5), self.log)
        if not ok:
            self.log.warning("Voice action failed: %s",
                             action.get("trigger", ""))
            return False
        message = self.tr("notify.action_ran", name=action.get("trigger", ""))
        self.log.info("Voice action ran: %s", action.get("trigger", ""))
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        return True

    def inserted_message(self, text: str) -> str:
        """Notification text: the character count, the phrase on opt-in."""
        if self.cfg.get("notify_text", False):
            short = text[:80] + ("…" if len(text) > 80 else "")
            return self.tr("notify.inserted", text=short)
        return self.tr("notify.inserted_chars", n=len(text))

    # -- panel draft -------------------------------------------------------
    def _show_draft(self, text: str, wav: bytes | None = None,
                    run_hook: bool = True,
                    uncertain: list[str] | None = None,
                    offer: bool = False) -> None:
        """Hand the recognized text to the panel for review and insertion.

        ``uncertain`` names the words the engine doubted; the HUD marks them
        inside the draft. The text is the reviewed one either way.

        ``offer`` says this take is doubtful, so the HUD may offer the wider
        pass (card-247) while the take's WAV is held; a draft shown for any
        other reason keeps the offer off. Every new draft starts with no
        rollback: the wider pass never reaches across takes.
        """
        self._exit_history()
        self.live["partial"] = ""
        self.live["level"] = 0.0
        self.live["draft"] = text
        self.live["draft_uncertain"] = list(uncertain or [])
        self.live["draft_offer"] = bool(offer)
        self.live["draft_rollback"] = False
        self.live["draft_audio"] = wav is not None
        self._draft_prev_text = ""
        self._draft_prev_uncertain = []
        self._draft_prev_offer = False
        self._draft_wav = wav
        self._touch_draft()
        self.set_last(self.tr("hud.draft_ready"))
        self.log.info("Panel draft ready: %d chars", len(text))
        if run_hook:
            self._run_hook(text)

    def _touch_draft(self) -> None:
        """Change the revision so the HUD re-reads the draft."""
        self.live["draft_rev"] = int(self.live.get("draft_rev") or 0) + 1

    def _clear_draft_offers(self) -> None:
        """Drop both draft offers and the held previous text.

        The offers are about one take's text: a new draft, a cancel, a
        history flip or an insert must never leave a button that would act
        on another take.
        """
        self.live["draft_offer"] = False
        self.live["draft_rollback"] = False
        self.live["draft_audio"] = self._draft_wav is not None
        self._draft_prev_text = ""
        self._draft_prev_uncertain = []
        self._draft_prev_offer = False

    def _forget_draft(self) -> None:
        self._exit_history()
        self.live["draft"] = ""
        self.live["draft_uncertain"] = []
        self._draft_wav = None
        self._clear_draft_offers()
        self._touch_draft()

    def cancel_draft(self) -> None:
        """Drop the panel draft; a history flip returns the set-aside draft."""
        if self._history_cursor is not None:
            saved = self._history_saved
            saved_wav = self._history_saved_wav
            saved_uncertain = self._history_saved_uncertain
            self._exit_history()
            if saved:
                self.live["draft"] = saved
                self.live["draft_uncertain"] = list(saved_uncertain)
                self._draft_wav = saved_wav
                self._clear_draft_offers()
                self._touch_draft()
                self.set_last(self.tr("hud.draft_ready"))
                return
        if not self.live.get("draft"):
            return
        self._forget_draft()
        self.set_last(self.tr("notify.draft_cancelled"))
        self.log.info("Panel draft cancelled")

    # -- draft retry (card-247) ---------------------------------------------
    def retranscribe_draft(self) -> str:
        """Re-recognize the panel draft's take with a wider search.

        The HUD calls this on a click; the pass runs in a worker thread, so
        the panel never waits on the model. Only the engine's own text
        replaces the draft: the take's WAV is decoded again and the same
        post-filter runs, but the dictionary, snippets, actions, text rules
        and hook never see this pass. The text before it is kept for
        ``rollback_draft``.

        Refused while a take is being recorded, recognized or loaded, while
        an insert or another retry is in flight, and when the take's audio is
        not held. Returns "" — the new text arrives through the live draft.
        """
        if self.state in (STATE_LOADING, STATE_RECORDING, STATE_PROCESSING,
                          STATE_UPDATING):
            return self._retry_refused(self.tr("notify.wait_cycle"))
        if self._retry_busy or self._draft_busy:
            self.log.info("Draft retry ignored: a draft action is in flight")
            self._touch_draft()
            return ""
        if not str(self.live.get("draft") or "").strip():
            return ""
        if self._draft_wav is None:
            return self._retry_refused(
                self.tr("notify.retranscribe_no_audio"))
        self._retry_busy = True
        try:
            threading.Thread(target=self._retranscribe_draft_worker,
                             daemon=True).start()
        except Exception as e:  # a failed spawn is a notice
            self._retry_busy = False
            self.log.exception("Draft retry worker failed to start: %s", e)
            return self._retry_refused(self.tr("notify.retranscribe_failed"))
        return ""

    def _retry_refused(self, message: str) -> str:
        """A localized reason in the panel; a click that did nothing re-arms."""
        self.set_last(message)
        if self.live.get("draft"):
            self._touch_draft()
        return ""

    def _wav_samples(self, data: bytes) -> Any:
        """WAV bytes to 16 kHz float32 samples through ``audio.load_wav_16k``.

        The loader reads a path, so the holder's bytes land in a temporary
        file for exactly one read and are removed at once. A broken WAV is a
        log line and ``None``; the caller turns it into a notice.
        """
        import tempfile

        import audio as audio_mod
        fd, path = tempfile.mkstemp(prefix="vibeforma-retry-", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            return audio_mod.load_wav_16k(path)
        except Exception as e:  # noqa: BLE001 - a broken take is a notice
            self.log.warning("Draft audio unreadable: %s", e)
            return None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    def _retranscribe_draft_worker(self) -> None:
        """One wider pass over the draft's take, in a worker thread.

        The draft revision and the held bytes are captured first: a new
        take, a cancel or an insert that lands mid-pass makes the result
        stale, and a stale result is dropped instead of replacing another
        take's text. A failed pass leaves the draft exactly as it was and
        says so in the status line; the retry stays available.
        """
        gen = int(self.live.get("draft_rev") or 0)
        wav = self._draft_wav
        old_text = str(self.live.get("draft") or "")
        old_uncertain = list(self.live.get("draft_uncertain") or [])
        old_offer = bool(self.live.get("draft_offer"))
        failed = False
        try:
            if self.state in (STATE_LOADING, STATE_RECORDING,
                              STATE_PROCESSING, STATE_UPDATING):
                self.set_last(self.tr("notify.wait_cycle"))
                failed = True
                return
            if wav is None or self.asr is None:
                self.set_last(self.tr("notify.retranscribe_no_audio"))
                failed = True
                return
            samples = self._wav_samples(wav)
            if samples is None:
                self.set_last(self.tr("notify.retranscribe_failed"))
                failed = True
                return
            if samples.shape[0] == 0:
                self.set_last(self.tr("notify.retranscribe_no_speech"))
                failed = True
                return
            text = str(self.asr.retranscribe(samples) or "").strip()
            rows = list(getattr(self.asr, "last_segments", None) or [])
            if not text:
                self.set_last(self.tr("notify.retranscribe_no_speech"))
                failed = True
                return
            if (int(self.live.get("draft_rev") or 0) != gen
                    or self._draft_wav is not wav):
                self.log.info("Draft retry dropped: the draft changed mid-pass")
                return
            uncertain = transcript.uncertain_words(rows)
            self._draft_prev_text = old_text
            self._draft_prev_uncertain = old_uncertain
            self._draft_prev_offer = old_offer
            self.live["draft"] = text
            self.live["draft_uncertain"] = uncertain
            self.live["draft_offer"] = bool(
                transcript.needs_review(rows) or uncertain)
            self.live["draft_rollback"] = True
            self.live["draft_audio"] = True
            self._touch_draft()
            self.set_last(self.tr("draft.retranscribed", n=len(text)))
            self.log.info("Draft retranscribed: %d chars", len(text))
        except Exception as e:
            self.log.exception("Draft retranscription failed: %s", e)
            self.set_last(self.tr("notify.retranscribe_failed"))
            failed = True
        finally:
            self._retry_busy = False
            if (failed
                    and int(self.live.get("draft_rev") or 0) == gen):
                # Re-render the same text so the HUD re-arms its buttons.
                self._touch_draft()

    def rollback_draft(self) -> str:
        """Restore the draft's text from before the last wider pass.

        The snapshot is exact: the same characters and the same doubtful
        words, byte for byte. The take's WAV is kept, so the wider pass may
        be offered again when the restored take was doubtful; the rollback
        offer itself is spent. Returns the restored text, or "" when there
        is nothing to roll back to.
        """
        if not self.live.get("draft_rollback"):
            return ""
        if self._draft_busy or self._retry_busy:
            self.log.info("Rollback ignored: a draft action is in flight")
            return ""
        text = self._draft_prev_text
        uncertain = list(self._draft_prev_uncertain)
        offer = bool(self._draft_prev_offer)
        self.live["draft"] = text
        self.live["draft_uncertain"] = uncertain
        self.live["draft_offer"] = offer
        self.live["draft_rollback"] = False
        self.live["draft_audio"] = self._draft_wav is not None
        self._draft_prev_text = ""
        self._draft_prev_uncertain = []
        self._draft_prev_offer = False
        self._touch_draft()
        self.set_last(self.tr("draft.rolled_back"))
        self.log.info("Draft rolled back: %d chars", len(text))
        return text

    # -- history browsing ---------------------------------------------------
    def browse_history(self, step: int) -> None:
        """Flip through saved dictations: -1 older, +1 newer.

        The first flip sets the live draft aside; "newer" past the newest
        record brings it back. Insert / Edit / Copy / Cancel work on the
        shown record like on a fresh draft.
        """
        if self.state in (STATE_RECORDING, STATE_PROCESSING):
            self.log.info("History browsing during a dictation — ignored")
            return
        if self._draft_busy:
            self.log.info("History browsing during an insert — ignored")
            return
        if not self.cfg.get("history", False):
            self.set_last(self.tr("history.off"))
            return
        records = self.history_records()
        if not records:
            self.set_last(self.tr("history.empty"))
            return
        newest = list(reversed(records))
        if self._history_cursor is None:
            if step > 0:
                self.set_last(self.tr("history.at_newest"))
                return
            self._history_saved = str(self.live.get("draft") or "")
            self._history_saved_wav = self._draft_wav
            self._history_saved_uncertain = list(
                self.live.get("draft_uncertain") or [])
            self._history_total = len(newest)
            self._history_cursor = 0
        else:
            nxt = self._history_cursor + (1 if step < 0 else -1)
            if nxt < 0:
                saved = self._history_saved
                saved_wav = self._history_saved_wav
                saved_uncertain = self._history_saved_uncertain
                self._exit_history()
                if saved:
                    self.live["draft"] = saved
                    self.live["draft_uncertain"] = list(saved_uncertain)
                    self._draft_wav = saved_wav
                    self._clear_draft_offers()
                    self._touch_draft()
                    self.set_last(self.tr("hud.draft_ready"))
                else:
                    self._forget_draft()
                    self.set_last(self.tr("history.closed"))
                return
            if nxt >= len(newest):
                self.set_last(self.tr("history.at_oldest"))
                return
            self._history_cursor = nxt
        record = newest[self._history_cursor]
        self.live["draft"] = str(record.get("text") or "")
        # A browsed entry is not this draft's take: never attach the open
        # take's sound or its doubtful words to a stored record, and never
        # leave a retry or rollback button pointing at another take.
        self.live["draft_uncertain"] = []
        self._draft_wav = None
        self._clear_draft_offers()
        self._touch_draft()
        self.set_last(self.tr("history.browsing",
                              n=self._history_cursor + 1,
                              total=self._history_total))
        self.log.info("History draft: %d of %d", self._history_cursor + 1,
                      self._history_total)

    def _exit_history(self) -> None:
        self._history_cursor = None
        self._history_saved = ""
        self._history_saved_uncertain = []
        self._history_saved_wav = None
        self._history_total = 0

    # -- retranscription ----------------------------------------------------
    def retranscribe_last(self, model: str = "", prompt: str = "") -> str:
        """Re-recognize the newest dictation with another model and prompt.

        The take comes from the WAV saved beside the newest history entry
        (the ``history_audio`` option). A temporary ASR is built for
        ``model`` — the configured one when empty — and released after the
        pass, so the live engine and its loaded weights are never touched.
        ``prompt`` becomes the initial prompt; empty keeps the configured
        one. The new text waits in the panel draft for review.

        Returns the new text, or "" when the take is missing, the model
        fails or nothing was recognized; the reason lands in the panel
        status line either way.
        """
        if not self.cfg.get("history", False):
            return self._retranscribe_notice(self.tr("history.off"))
        if self.state in (STATE_RECORDING, STATE_PROCESSING, STATE_LOADING):
            return self._retranscribe_notice(self.tr("notify.wait_cycle"))
        records = self.history.load()
        if not records:
            return self._retranscribe_notice(self.tr("history.empty"))
        path = self.history.record_audio_path(records[-1])
        if not path or not os.path.isfile(path):
            return self._retranscribe_notice(self.tr("history.no_audio"))
        import audio as audio_mod
        try:
            samples = audio_mod.load_wav_16k(path)
        except Exception as e:  # noqa: BLE001 - a broken WAV is a notice
            self.log.warning("History audio unreadable: %s", e)
            return self._retranscribe_notice(self.tr("history.audio_broken"))
        wanted = str(model or "").strip() or str(self.cfg.get("model") or "")
        import asr as asr_mod
        temp = asr_mod.ASR({**self.cfg, "model": wanted}, self.log)
        try:
            temp.load()
            t0 = time.monotonic()
            segments = temp.transcribe_array(
                samples, len(samples) / float(audio_mod.SAMPLE_RATE),
                language=str(self.cfg.get("language") or "") or None,
                initial_prompt=str(prompt or "").strip() or None)
            text = "".join(seg.text for seg in segments).strip()
            took = time.monotonic() - t0
        except Exception as e:  # a failed model is a notice, not a crash
            self.log.exception("Retranscription failed: %s", e)
            return self._retranscribe_notice(
                errors.last_line(self.cfg, "recognition", e))
        finally:
            # One dictation-sized pass only: the temporary weights go now.
            with contextlib.suppress(Exception):
                temp.model = None
            gc.collect()
        if not text:
            return self._retranscribe_notice(
                self.tr("notify.not_recognized_short"))
        self.log.info("Retranscribed the last dictation: %d chars in %.1f s, "
                      "model=%s", len(text), took, wanted)
        self._show_draft(text)
        message = self.tr("history.retranscribed", n=len(text))
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        return text

    def _retranscribe_notice(self, message: str) -> str:
        """Panel status and tray notice for a pass that produced no text."""
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        return ""

    def retranscribe_last_dialog(self) -> None:
        """Tray: ask for the prompt, then re-recognize the newest take.

        The dialog runs in its own thread with its own Tk root, like the file
        dialogs, so the HUD loop and the hotkey stay free. Cancelling the
        dialog does nothing; the model is the configured one, so another
        model is picked in the tray before the pass.
        """
        threading.Thread(target=self._retranscribe_last_dialog_worker,
                         daemon=True).start()

    def _retranscribe_last_dialog_worker(self) -> None:
        try:
            import tkinter as tk
            from tkinter import simpledialog

            root = tk.Tk()
            root.withdraw()
            try:
                prompt = simpledialog.askstring(
                    self.tr("dialog.retranscribe_prompt"),
                    self.tr("dialog.retranscribe_prompt_hint"),
                    initialvalue=str(self.cfg.get("hotwords") or ""),
                    parent=root)
            finally:
                root.destroy()
        except Exception as e:  # noqa: BLE001 - a broken dialog must not kill the HUD
            self.log.warning("Retranscribe dialog failed: %s", e)
            return
        if prompt is None:   # the person cancelled the dialog
            return
        self.retranscribe_last(prompt=prompt)

    def copy_draft(self, text: str) -> None:
        """Put the reviewed panel text into the clipboard and close it."""
        text = (text or "").strip()
        if not text:
            return
        import insert as insert_mod
        insert_mod.copy_text(text)
        if self.cfg.get("clipboard_exclude_history", True):
            insert_mod.exclude_from_clipboard_history()
        wav = getattr(self, "_draft_wav", None)
        self._remember(text, way=history.WAY_COPY, wav=wav)
        self._forget_draft()
        message = self.tr("history.copied")
        self.set_last(message)
        if self.tray:
            self.tray.notify(message)
        self.log.info("Panel draft copied: %d chars", len(text))

    def insert_draft(self, text: str, target_hwnd: int | None = None) -> None:
        """Paste the reviewed panel text into the window the user picked.

        The draft as it was recognized is remembered next to the final
        text: the worker learns the person's replacements from the pair.
        """
        text = (text or "").strip()
        if not text or self._draft_busy:
            return
        original = str(self.live.get("draft") or "")
        self._draft_busy = True
        threading.Thread(target=self._insert_draft_worker,
                         args=(text, target_hwnd, original), daemon=True).start()

    def _insert_draft_worker(self, text: str, target_hwnd: int | None,
                             original: str | None = None) -> None:
        import insert as insert_mod
        if original is not None and original.strip() != text:
            self._learn_correction(original, text)
        try:
            if not target_hwnd or not insert_mod.activate_window(target_hwnd):
                insert_mod.copy_text(text)
                self._forget_draft()
                message = self.tr("notify.draft_copied")
                self.set_last(message)
                if self.tray:
                    self.tray.notify(message)
                self.log.info("Panel draft copied: no insertion target")
                return
            title_wanted = (self.cfg.get("log_window_titles", False)
                            or self.cfg.get("history_app", False))
            title = insert_mod.foreground_window() if title_wanted else ""
            undo_state: dict[str, Any] = {}
            result = insert_mod.insert_text(
                text, restore_clipboard=self.cfg.get("restore_clipboard", True),
                log=self.log, target_hwnd=target_hwnd,
                paste_method=self.cfg.get("paste_method", "shift_insert"),
                exclude_history=self.cfg.get("clipboard_exclude_history", True),
                log_title=self.cfg.get("log_window_titles", False),
                undo_state=undo_state)
            if result != insert_mod.RESULT_PASTED:
                self._forget_draft()
                message = self.tr("notify.draft_copied")
                self.set_last(message)
                if self.tray:
                    self.tray.notify(message)
                return
            wav = self._draft_wav
            self._forget_draft()
            self._target_hwnd = target_hwnd
            self._target_title = title or ""
            self._last_insert = {
                "text": text,
                "hwnd": target_hwnd,
                "ts": time.time(),
                "prev": undo_state.get("prev"),
                "prev_known": bool(undo_state.get("prev_known")),
            }
            self._remember(text, wav=wav)
            message = self.inserted_message(text)
            self.set_last(message)
            if self.tray:
                self.tray.notify(message)
        except Exception as e:
            self.log.exception("Panel insert error: %s", e)
            self.set_last(errors.last_line(self.cfg, "recognition", e))
            self._touch_draft()   # keep the text in the panel for another try
        finally:
            self._draft_busy = False

    def _learn_correction(self, original: str, edited: str) -> None:
        """Remember the words the person replaced in the draft, locally.

        Only the changed words go to corrections.json; the full text is
        never stored or logged. A broken store or a failed write is a line
        in the log, never a broken insertion.
        """
        try:
            import dictlearn
            pairs = dictlearn.collect_correction(original, edited)
            if pairs:
                self.log.info("Dictionary corrections: %d pair(s)", len(pairs))
        except Exception as e:  # noqa: BLE001 - learning must not break insertion
            self.log.warning("Dictionary corrections: %s", e)

    # -- file mode ---------------------------------------------------------
    def start_pipe(self) -> bool:
        """Start the CLI channel before the model load.

        The pipe must answer during loading, when ``location.json`` already
        names the running service: a caller gets a defined "loading" reply
        instead of "VibeForma is not running". Returns whether it listens.
        """
        if self.pipe is not None:
            return True
        try:
            from .pipe import PipeServer
            server = PipeServer(self._pipe_request, self.log)
            if server.start():
                self.pipe = server
                self.log.info("CLI pipe listening: %s", server.name)
                return True
            self.log.info("CLI pipe: not available on this platform")
        except Exception as e:  # noqa: BLE001 - the pipe must not stop dictation
            self.log.warning("CLI pipe not started: %s", e)
        self.pipe = None
        return False

    def start_file_mode(self) -> None:
        """Create the job queue once the model is ready; the pipe is older."""
        if self.file_jobs is not None:
            return
        from file_jobs import FileJobs
        self.file_jobs = FileJobs(self.cfg, self.asr, self.log,
                                  on_event=self._on_file_event,
                                  on_batch_event=self._on_batch_event)

    def _on_file_event(self, job: Any) -> None:
        """One live view for the HUD and the tray, never the transcript text."""
        payload = {
            "id": job.id,
            "batch_id": job.batch_id,
            "state": job.state,
            "progress": round(float(job.progress), 1),
            "segments": len(job.segments),
            "name": os.path.basename(job.source),
            "relative": job.relative,
            "error": job.error or "",
            "finished_at": float(job.finished_at or 0.0),
        }
        previous = self.live.get("file_job") or {}
        previous_id = str(previous.get("id") or "")
        previous_batch = str(previous.get("batch_id") or "")
        previous_state = str(previous.get("state") or "")
        previous_live_single = (bool(previous_id) and not previous_batch
                                and previous_state in
                                ("queued", "running", "paused"))
        same = previous_id == job.id
        if job.batch_id:
            # A batch child never overwrites a live single-file slot: the
            # batch snapshot carries the current name.
            take = same or not previous_id or bool(previous_batch)
        else:
            take = (same or not previous_id or bool(previous_batch)
                    or not previous_live_single
                    or job.state in ("queued", "running", "paused"))
        if take:
            self.live["file_job"] = payload
        if job.outputs:
            self._last_file_outputs = list(job.outputs)
        # A batch child reports through the batch: one notification, not one
        # per file, so a folder of twenty files is not twenty popups.
        if not job.batch_id and job.state in ("done", "error", "cancelled"):
            if job.state == "done" and job.outputs:
                self.set_last(self.tr("notify.file_done",
                                      path=job.outputs[0]))
                if self.tray:
                    self.tray.notify(self.tr("notify.file_done",
                                             path=job.outputs[0]))
            elif job.state == "error":
                self.set_last(self.tr("notify.file_failed",
                                      error=job.error or ""))
                if self.tray:
                    self.tray.notify(self.tr("notify.file_failed",
                                             error=job.error or ""))
        if self.tray:
            with contextlib.suppress(Exception):
                self.tray.set_state(self.state)
        if job.state in ("done", "error", "cancelled", "skipped"):
            self._maybe_apply_pending_update()

    def _on_batch_event(self, batch: Any) -> None:
        """The batch snapshot the HUD panel reads; one notice at the end."""
        previous = self.live.get("file_batch") or {}
        counts = {"total": int(batch.total or 0), "done": 0, "skipped": 0,
                  "failed": 0, "cancelled": 0, "pending": 0}
        current_progress = 0.0
        jobs = getattr(self, "file_jobs", None)
        if jobs is not None:
            with contextlib.suppress(Exception):
                counts = jobs.counts(batch)
            if batch.current:
                with contextlib.suppress(Exception):
                    current = jobs.get(batch.current)
                    if current is not None:
                        current_progress = float(current.progress)
        payload = {
            "id": batch.id,
            "batch_id": batch.id,
            "state": batch.state,
            "total": int(counts["total"]),
            "done": int(counts["done"]),
            "skipped": int(counts["skipped"]),
            "failed": int(counts["failed"]),
            "cancelled": int(counts["cancelled"]),
            "pending": int(counts["pending"]),
            "progress": round(float(batch.progress), 1),
            "current": batch.current,
            "current_name": batch.current_name,
            "current_progress": round(current_progress, 1),
            "error": batch.error,
            "finished_at": float(batch.finished_at or 0.0),
        }
        ended = batch.state in ("done", "cancelled")
        was_ended = (previous.get("id") == batch.id
                     and previous.get("state") in ("done", "cancelled"))

        def _rank(state: str) -> int:
            if state in ("running", "paused"):
                return 2
            if state == "queued":
                return 1
            return 0

        same_id = previous.get("id") == batch.id
        incoming_rank = _rank(batch.state)
        previous_rank = _rank(str(previous.get("state") or ""))
        take = (same_id or not previous.get("id")
                or incoming_rank > previous_rank)
        if not take and incoming_rank == previous_rank == 0:
            take = (float(payload["finished_at"])
                    >= float(previous.get("finished_at") or 0.0))
        if take:
            self.live["file_batch"] = payload
        if ended and not was_ended:
            if batch.cancelled:
                message = self.tr("notify.batch_cancelled")
            elif counts["failed"]:
                message = self.tr("notify.batch_failed",
                                  done=counts["done"],
                                  skipped=counts["skipped"],
                                  failed=counts["failed"])
            else:
                message = self.tr("notify.batch_done", done=counts["done"],
                                  skipped=counts["skipped"],
                                  failed=counts["failed"])
            self.set_last(message)
            if self.tray:
                self.tray.notify(message)
            self._maybe_apply_pending_update()

    def has_active_batch(self) -> bool:
        jobs = self.file_jobs
        if jobs is None:
            return False
        with contextlib.suppress(Exception):
            return bool(jobs.active_batches())
        return False

    def cancel_batch(self) -> int:
        """Tray: stop every unfinished batch; the number actually stopped."""
        jobs = self.file_jobs
        if jobs is None:
            return 0
        with contextlib.suppress(Exception):
            return int(jobs.cancel_batches())
        return 0

    def transcribe_file_dialog(self) -> None:
        """Tray: the person picks a file, then it joins the queue.

        The dialog runs in its own thread with its own Tk root, so the HUD
        loop and the hotkey stay free; the person's choice is consent and is
        not bound by ``file_roots``.
        """
        threading.Thread(target=self._transcribe_file_dialog_worker,
                         daemon=True).start()

    def _transcribe_file_dialog_worker(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            from file_jobs import SUPPORTED_MEDIA
            media = " ".join("*" + ext for ext in SUPPORTED_MEDIA)
            root = tk.Tk()
            root.withdraw()
            try:
                path = filedialog.askopenfilename(
                    title=self.tr("dialog.transcribe_file"),
                    filetypes=[
                        (self.tr("dialog.media"), media),
                        (self.tr("dialog.all_files"), "*.*"),
                    ])
            finally:
                root.destroy()
        except Exception as e:  # noqa: BLE001 - a broken dialog must not kill the HUD
            self.log.warning("File dialog failed: %s", e)
            return
        if not path:
            return
        if self.file_jobs is None:
            self.set_last(self.tr("notify.file_not_ready"))
            if self.tray:
                self.tray.notify(self.tr("notify.file_not_ready"))
            return
        try:
            self.file_jobs.submit(path, language=self.cfg.get("language") or "",
                                  formats=["srt", "txt"], trusted=True)
        except Exception as e:  # noqa: BLE001 - a refused path is a notice
            self.log.warning("File job refused: %s", e)
            self.set_last(self.tr("notify.file_failed", error=str(e)))
            if self.tray:
                self.tray.notify(self.tr("notify.file_failed", error=str(e)))

    def transcribe_folder_dialog(self) -> None:
        """Tray: the person picks a folder, then every media file joins.

        The dialog runs in its own thread with its own Tk root, like the
        single-file one; the person's choice is consent, so ``trusted`` is
        set and ``file_roots`` does not apply. Locality still does.
        """
        threading.Thread(target=self._transcribe_folder_dialog_worker,
                         daemon=True).start()

    def _transcribe_folder_dialog_worker(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                folder = filedialog.askdirectory(
                    mustexist=True,
                    title=self.tr("dialog.transcribe_folder"))
            finally:
                root.destroy()
        except Exception as e:  # noqa: BLE001 - a broken dialog must not kill the HUD
            self.log.warning("Folder dialog failed: %s", e)
            return
        if not folder:
            return
        if self.file_jobs is None:
            self.set_last(self.tr("notify.file_not_ready"))
            if self.tray:
                self.tray.notify(self.tr("notify.file_not_ready"))
            return
        from file_jobs import NoMediaFiles
        try:
            self.file_jobs.submit_folder(
                folder,
                language=self.cfg.get("language") or "",
                formats=["srt", "txt"],
                recursive=bool(self.cfg.get("file_folder_recursive", False)),
                trusted=True)
        except NoMediaFiles:
            self.set_last(self.tr("notify.folder_empty"))
            if self.tray:
                self.tray.notify(self.tr("notify.folder_empty"))
        except Exception as e:  # noqa: BLE001 - a refused folder is a notice
            self.log.warning("Folder job refused: %s", e)
            message = self.tr("notify.folder_failed", error=str(e))
            self.set_last(message)
            if self.tray:
                self.tray.notify(message)

    def last_file_folder(self) -> str:
        """The folder of the last finished job, for the tray's open action."""
        if not self._last_file_outputs:
            return ""
        return os.path.dirname(self._last_file_outputs[0])

    def open_last_file_folder(self) -> None:
        folder = self.last_file_folder()
        if not folder:
            return
        with contextlib.suppress(Exception):
            os.startfile(folder)  # noqa: S606 - the user asked to see the result

    def _pipe_request(self, request: dict[str, Any],
                      send: Any) -> dict[str, Any]:
        """One CLI call over the pipe: submit, stream progress, answer."""
        command = str(request.get("command") or "")
        if command == "transcribe":
            return self._transcribe_file_request(request, send)
        if command == "transcribe_folder":
            return self._transcribe_folder_request(request, send)
        if command == "cancel_batch":
            return self._cancel_batch_request(request)
        return {"event": "error", "ok": False, "code": 2,
                "error": "unknown command"}

    def _transcribe_file_request(self, request: dict[str, Any],
                                 send: Any) -> dict[str, Any]:
        if self.file_jobs is None:
            return {"event": "error", "ok": False, "code": 1, "loading": True,
                    "error": "the model is loading"}
        formats = [str(f).lower() for f in request.get("formats") or []]
        import asr as asr_mod
        from file_jobs import FilePathError
        try:
            job = self.file_jobs.submit(
                str(request.get("path") or ""),
                language=str(request.get("language") or ""),
                model=str(request.get("model") or ""),
                formats=formats,
                out=str(request.get("out") or ""),
                force=bool(request.get("force")),
                prompt=str(request.get("prompt") or ""),
                word_timestamps=(bool(request.get("word_timestamps"))
                                 if request.get("word_timestamps") is not None
                                 else None),
                clip_timestamps=str(request.get("clip_timestamps") or ""),
            )
        except asr_mod.ModelNotInstalled as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except FilePathError as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except ValueError as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except OSError as e:
            self.log.warning("CLI file job refused: %s", e)
            return {"event": "error", "ok": False, "code": 1, "error": str(e)}
        while True:
            send({"event": "progress", "job_id": job.id, "state": job.state,
                  "progress": round(float(job.progress), 1)})
            if job.state in ("done", "error", "cancelled"):
                break
            time.sleep(0.3)
        if job.state == "done":
            return {"event": "done", "ok": True, "job_id": job.id,
                    "out": list(job.outputs),
                    "segments": len(job.segments),
                    "duration_s": round(float(job.duration), 3)}
        if job.state == "cancelled":
            return {"event": "error", "ok": False, "cancelled": True,
                    "job_id": job.id, "error": "cancelled"}
        return {"event": "error", "ok": False, "job_id": job.id,
                "error": job.error or job.state}

    def _transcribe_folder_request(self, request: dict[str, Any],
                                   send: Any) -> dict[str, Any]:
        """Submit one folder, stream the batch counters, answer with a summary."""
        if self.file_jobs is None:
            return {"event": "error", "ok": False, "code": 1, "loading": True,
                    "error": "the model is loading"}
        import asr as asr_mod
        from file_jobs import FilePathError, NoMediaFiles
        formats = [str(f).lower() for f in request.get("formats") or []]
        try:
            batch = self.file_jobs.submit_folder(
                str(request.get("folder") or ""),
                language=str(request.get("language") or ""),
                formats=formats,
                out=str(request.get("out") or ""),
                recursive=bool(request.get("recursive")),
                force=bool(request.get("force")),
            )
        except NoMediaFiles as e:
            self.log.warning("CLI folder job refused: %s", e)
            return {"event": "error", "ok": False, "code": 4,
                    "error": str(e) or "nothing to transcribe"}
        except (asr_mod.ModelNotInstalled, FilePathError) as e:
            self.log.warning("CLI folder job refused: %s", e)
            return {"event": "error", "ok": False, "code": 2, "error": str(e)}
        except (OSError, ValueError) as e:
            self.log.warning("CLI folder job refused: %s", e)
            return {"event": "error", "ok": False, "code": 1, "error": str(e)}
        last: tuple[Any, ...] | None = None
        while True:
            counts = self.file_jobs.counts(batch)
            snapshot = (batch.state, counts["done"], counts["skipped"],
                        counts["failed"], counts["cancelled"],
                        int(round(float(batch.progress))))
            if snapshot != last:
                last = snapshot
                file_progress = 0.0
                if batch.current:
                    with contextlib.suppress(Exception):
                        current = self.file_jobs.get(batch.current)
                        if current is not None:
                            file_progress = float(current.progress)
                send({"event": "progress", "batch_id": batch.id,
                      "state": batch.state, "total": counts["total"],
                      "done": counts["done"], "skipped": counts["skipped"],
                      "failed": counts["failed"],
                      "cancelled": counts["cancelled"],
                      "pending": counts["pending"],
                      "progress": round(float(batch.progress), 1),
                      "current": batch.current,
                      "file": batch.current_name,
                      "file_progress": round(file_progress, 1)})
            if batch.state in ("done", "cancelled"):
                break
            time.sleep(0.3)
        summary = self._batch_summary(batch)
        if batch.cancelled:
            return {"event": "error", "ok": False, "cancelled": True,
                    "batch_id": batch.id, "code": 3, "error": "cancelled",
                    "summary": summary}
        return {"event": "done", "ok": summary["ok"], "batch_id": batch.id,
                "summary": summary}

    def _batch_summary(self, batch: Any) -> dict[str, Any]:
        """The JSON summary the CLI prints: every file, in walk order."""
        jobs = self.file_jobs
        counts = {"total": int(batch.total or 0), "done": 0, "skipped": 0,
                  "failed": 0, "cancelled": 0, "pending": 0}
        if jobs is not None:
            with contextlib.suppress(Exception):
                counts = jobs.counts(batch)
        files: list[dict[str, Any]] = []
        for child_id in batch.children:
            job = None
            if jobs is not None:
                with contextlib.suppress(Exception):
                    job = jobs.get(child_id)
            if job is None:
                continue
            files.append({
                "source": job.source,
                "relative": job.relative,
                "state": job.state,
                "outputs": list(job.outputs),
                "segments": len(job.segments),
                "duration_s": round(float(job.duration), 3),
                "error": job.error,
            })
        return {
            "ok": counts["failed"] == 0 and counts["cancelled"] == 0,
            "batch_id": batch.id,
            "folder": batch.root,
            "out": batch.out,
            "recursive": bool(batch.recursive),
            "total": int(counts["total"]),
            "done": int(counts["done"]),
            "skipped": int(counts["skipped"]),
            "failed": int(counts["failed"]),
            "cancelled": int(counts["cancelled"]),
            "progress": round(float(batch.progress), 1),
            "files": files,
        }

    def _cancel_batch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.file_jobs is None:
            return {"ok": False, "cancelled": 0, "code": 1,
                    "error": "the model is loading"}
        batch_id = str(request.get("batch_id") or "")
        if batch_id:
            stopped = 1 if self.file_jobs.cancel_batch(batch_id) else 0
        else:
            stopped = self.file_jobs.cancel_batches()
        return {"ok": True, "cancelled": int(stopped)}

    # -- startup -----------------------------------------------------------
    def load_model_async(self) -> None:
        def _load() -> None:
            try:
                import asr as asr_mod
                from faster_whisper.vad import get_vad_model
                self.asr = asr_mod.ASR(self.cfg, self.log)
                self.asr.load()
                self.log.info("Loading Silero VAD…")
                self.vad = get_vad_model()
                self.log.info("Warm-up (short empty transcription)…")
                import numpy as np
                self.asr.transcribe(np.zeros((16000,), dtype=np.float32))
                self.log.info("Warm-up done — service ready")
                self.start_file_mode()
                self._set_state(STATE_IDLE)
                if self.tray:
                    self.tray.notify(self.tr(
                        "notify.ready",
                        hotkey=self.cfg["hotkey"].replace("+", "+").upper()))
            except Exception as e:
                self.log.exception("Model failed to load: %s", e)
                self._set_state(STATE_ERROR)
                if self.tray:
                    self.tray.notify(errors.text(self.cfg, "model", e))
        threading.Thread(target=_load, daemon=True).start()

    def shutdown(self) -> None:
        self._running = False
        self.live["exit"] = True
        if self.pipe is not None:
            with contextlib.suppress(Exception):
                self.pipe.stop()
        if self.file_jobs is not None:
            with contextlib.suppress(Exception):
                self.file_jobs.shutdown()
        if self.hotkey:
            try:
                self.hotkey.stop()
            except Exception as e:  # noqa: BLE001 - listener teardown raises engine-specific errors
                self.log.debug("Hotkey stop: %s", e)
        if self.cancel_watcher:
            try:
                self.cancel_watcher.stop()
            except Exception as e:  # noqa: BLE001 - listener teardown raises engine-specific errors
                self.log.debug("Esc watcher stop: %s", e)
        if self.tray:
            self.tray.stop()
