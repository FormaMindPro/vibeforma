"""ASR hotwords: every window, target forms, frequency-ranked selection.

faster-whisper resets ``initial_prompt`` after every 30 s window, so a term
that lives there stops working from the second one. The dictionary travels in
``hotwords`` instead, which the library re-applies to each window. These
tests pin the three promises of card-237: the per-window parameter is sent on
every engine path, the prompt teaches the target form (``to``), and the token
budget is filled by correction frequency rather than dictionary order.
"""
import json

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first
import dictionary  # noqa: E402
import dictlearn  # noqa: E402


class FakeLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else str(fmt)))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def debug(self, fmt, *args):
        self._add("debug", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)

    def info_lines(self):
        return [line for level, line in self.lines if level == "info"]


class _Row:
    """A faster-whisper segment stand-in; ``seek`` names its 30 s window."""

    def __init__(self, start=0.0, end=1.0, text="", seek=0):
        self.start = start
        self.end = end
        self.text = text
        self.seek = seek
        self.words = None


class _Info:
    language = "ru"
    duration = 65.0


class FakeModel:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [_Row()]
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter(self.rows), _Info()


class _FakePipeline:
    """Stands in for BatchedInferencePipeline; records the single pass."""

    instances = []

    def __init__(self, model):
        self.model = model
        self.calls = []
        _FakePipeline.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter([_Row(0.5, 1.0, " batched")]), _Info()


@pytest.fixture(autouse=True)
def _empty_corrections(tmp_path, monkeypatch):
    """A developer's own corrections.json must not shape the test results."""
    monkeypatch.setattr(dictlearn, "DATA_DIR", str(tmp_path))


def _cfg(**over):
    cfg = {"language": "ru", "model": "fake", "hotwords": "", "dictionary": []}
    cfg.update(over)
    return cfg


def _engine(cfg=None, rows=None):
    log = FakeLog()
    engine = asr.ASR(cfg or _cfg(), log)
    engine.model = FakeModel(rows)
    return engine


def _audio(seconds=1.0):
    return np.zeros(int(16000 * seconds), dtype=np.float32)


# -- the library parameter, not the first-window-only prompt ----------------
def test_the_initial_prompt_carries_only_the_language_preset():
    prompt = asr._prompt(_cfg(hotwords="Forma",
                              dictionary=[{"from": "vibra",
                                           "to": "VibeForma"}]))
    assert prompt == asr.PROMPT_PRESETS["ru"]


def test_dictation_sends_the_target_forms_as_hotwords():
    engine = _engine(_cfg(hotwords="Forma",
                          dictionary=[{"from": "vibra", "to": "VibeForma"}]))

    engine.transcribe(_audio())

    _audio_out, kwargs = engine.model.calls[-1]
    assert kwargs["hotwords"] == "Forma, VibeForma"
    assert kwargs["initial_prompt"] == asr.PROMPT_PRESETS["ru"]
    assert "VibeForma" not in kwargs["initial_prompt"]
    assert "vibra" not in kwargs["hotwords"]


def test_each_window_of_a_long_dictation_logs_the_hotwords():
    rows = [_Row(0.5, 1.0, " first", seek=0),
            _Row(30.5, 31.0, " second", seek=3000),
            _Row(60.5, 61.0, " third", seek=6000)]
    engine = _engine(_cfg(dictionary=[{"from": "vibra", "to": "VibeForma"},
                                      {"from": "vibra ai", "to": "VibeAI"}]),
                     rows)

    text = engine.transcribe(_audio(65.0))

    window_lines = [line for line in engine.log.info_lines()
                    if line.startswith("ASR window")]
    assert window_lines == [
        "ASR window 1: hotwords 2 terms",
        "ASR window 2: hotwords 2 terms",
        "ASR window 3: hotwords 2 terms",
    ]
    assert text == "first second third"


def test_a_live_partial_sends_hotwords_without_window_lines():
    engine = _engine(_cfg(dictionary=[{"from": "vibra", "to": "VibeForma"}]))

    engine.transcribe(_audio(10.0), verbose=False, partial=True)

    _audio_out, kwargs = engine.model.calls[-1]
    assert kwargs["hotwords"] == "VibeForma"
    assert not [line for line in engine.log.info_lines()
                if line.startswith("ASR window")]


def test_the_file_sequential_path_sends_hotwords():
    engine = _engine(_cfg(dictionary=[{"from": "vibra", "to": "VibeForma"}]))

    list(engine.transcribe_array(_audio(2.0), 2.0, batched=False))

    _audio_out, kwargs = engine.model.calls[-1]
    assert kwargs["hotwords"] == "VibeForma"


def test_the_batched_file_path_sends_hotwords(monkeypatch):
    _FakePipeline.instances.clear()
    monkeypatch.setattr(asr, "BatchedInferencePipeline", _FakePipeline)
    engine = _engine(_cfg(dictionary=[{"from": "vibra", "to": "VibeForma"}]))

    list(engine.transcribe_array(_audio(2.0), 2.0, batched=True))

    assert engine.model.calls == []
    _audio_out, kwargs = _FakePipeline.instances[0].calls[0]
    assert kwargs["hotwords"] == "VibeForma"
    assert kwargs["initial_prompt"] == asr.PROMPT_PRESETS["ru"]


def test_an_explicit_prompt_does_not_swallow_the_hotwords():
    engine = _engine(_cfg(dictionary=[{"from": "vibra", "to": "VibeForma"}]))

    list(engine.transcribe_array(_audio(2.0), 2.0, initial_prompt="hint"))

    _audio_out, kwargs = engine.model.calls[-1]
    assert kwargs["initial_prompt"] == "hint"
    assert kwargs["hotwords"] == "VibeForma"


# -- selection: frequency first, then the token budget ----------------------
def test_corrections_json_ranks_the_terms(tmp_path):
    (tmp_path / "corrections.json").write_text(json.dumps({"pairs": [
        {"from": "alpha", "to": "Alpha", "count": 1},
        {"from": "vibra", "to": "VibeForma", "count": 9},
    ]}), encoding="utf-8")
    engine = _engine(_cfg(dictionary=[{"from": "alpha", "to": "Alpha"},
                                      {"from": "vibra", "to": "VibeForma"}]))

    engine.transcribe(_audio())

    _audio_out, kwargs = engine.model.calls[-1]
    assert kwargs["hotwords"].split(", ") == ["VibeForma", "Alpha"]


def test_five_hundred_pairs_keep_the_frequent_correction_within_the_budget(
        tmp_path):
    (tmp_path / "corrections.json").write_text(json.dumps({"pairs": [
        {"from": "vibra", "to": "VibeForma", "count": 9},
    ]}), encoding="utf-8")
    entries = [{"from": f"misspelling{i:03d}", "to": f"Term{i:03d}"}
               for i in range(dictionary.MAX_ENTRIES - 1)]
    entries.append({"from": "vibra", "to": "VibeForma"})
    engine = _engine(_cfg(dictionary=entries))

    engine.transcribe(_audio())

    _audio_out, kwargs = engine.model.calls[-1]
    hotwords = kwargs["hotwords"]
    terms = hotwords.split(", ")
    assert terms[0] == "VibeForma"
    assert dictionary.estimate_tokens(hotwords) <= dictionary.MAX_PROMPT
    assert "Term498" not in terms


class _Ids:
    def __init__(self, count):
        self.ids = list(range(count))


class _CharTokenizer:
    """One token per character, so the exact counting path is visible."""

    def encode(self, text):
        return _Ids(len(text))


def test_a_loaded_tokenizer_counts_exact_tokens_not_the_estimate():
    big = "x" * 100
    entries = [{"from": f"misspelling{i}", "to": big + str(i)}
               for i in range(5)]
    engine = _engine(_cfg(dictionary=entries))
    engine.model.hf_tokenizer = _CharTokenizer()

    engine.transcribe(_audio())

    _audio_out, kwargs = engine.model.calls[-1]
    assert kwargs["hotwords"].split(", ") == [big + "0", big + "1"]
