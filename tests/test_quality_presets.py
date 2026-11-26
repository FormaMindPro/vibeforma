"""Recognition quality presets (card-265).

Three presets set the final dictation pass only: normal keeps beam 5 and the
0.0 -> 0.2 fallback ladder, accurate uses beam 10 at one temperature, fast
decodes greedily at one temperature. A missing or unknown config value reads
as normal, the settings window saves the code, and no raw temperature control
sits in the panel.
"""
import json
import logging
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import asr  # noqa: E402 - asr imports numpy; skip the module first
import config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "settings_window.py"
LOCALES = ROOT / "app" / "locales"

RU_NORMAL = ("\u041e\u0431\u044b\u0447\u043d\u044b\u0439 "
             "(\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u0435"
             "\u0442\u0441\u044f)")
RU_ACCURATE = "\u0422\u043e\u0447\u043d\u044b\u0439"
RU_FAST = "\u0411\u044b\u0441\u0442\u0440\u044b\u0439"

NEW_KEYS = (
    "settings.g_quality",
    "settings.quality",
    "settings.quality_normal",
    "settings.quality_accurate",
    "settings.quality_fast",
    "settings.note_quality",
)


def _source():
    return SOURCE.read_text(encoding="utf-8")


def _method(text, name):
    block = text[text.index("def " + name):]
    return block.split("\n    def ")[0]


def _catalog(lang):
    with open(LOCALES / f"{lang}.json", encoding="utf-8") as handle:
        return json.load(handle)


# -- the mapping -----------------------------------------------------------
def test_the_default_is_normal():
    assert config.QUALITY_PRESET_DEFAULT == "normal"
    assert config.QUALITY_PRESETS == ("normal", "accurate", "fast")
    assert config.DEFAULTS["quality_preset"] == "normal"


def test_an_old_config_without_the_key_loads_normal(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"language": "ru"}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))

    cfg = config.load()

    assert cfg["quality_preset"] == "normal"
    assert cfg["language"] == "ru"


@pytest.mark.parametrize(("preset", "beam", "temperature"), [
    ("normal", 5, (0.0, 0.2)),
    ("accurate", 10, 0.0),
    ("fast", 1, 0.0),
])
def test_each_preset_maps_to_its_beam_and_temperature(
        preset, beam, temperature):
    assert asr.quality_preset({"quality_preset": preset}) == (
        preset, beam, temperature)


@pytest.mark.parametrize("cfg", [
    {},
    {"quality_preset": None},
    {"quality_preset": ""},
    {"quality_preset": "turbo"},
    {"quality_preset": 7},
])
def test_a_missing_or_invalid_value_reads_as_normal(cfg):
    assert asr.quality_preset(cfg) == ("normal", 5, (0.0, 0.2))


def test_the_preset_name_is_trimmed_and_casefolded():
    assert asr.quality_preset({"quality_preset": " Accurate "}) == (
        "accurate", 10, 0.0)


# -- the final dictation pass ----------------------------------------------
SAMPLE_RATE = 16000


class _Info:
    language = "en"


class _Segment:
    def __init__(self, text=" hello"):
        self.seek = 0
        self.start = 0.0
        self.end = 1.0
        self.text = text
        self.words = None
        self.avg_logprob = -0.2
        self.compression_ratio = 1.1
        self.no_speech_prob = 0.05
        self.temperature = 0.0


class _Model:
    def __init__(self):
        self.calls = []

    def transcribe(self, _audio, **kwargs):
        self.calls.append(kwargs)
        return [_Segment()], _Info()


def _engine(quality=None):
    cfg = {"language": "en", "hotwords": "", "dictionary": []}
    if quality is not None:
        cfg["quality_preset"] = quality
    engine = asr.ASR(cfg, logging.getLogger("test_quality_presets"))
    model = _Model()
    engine.model = model
    return engine, model


def _audio():
    return np.zeros(SAMPLE_RATE, dtype=np.float32)


@pytest.mark.parametrize(("preset", "beam", "temperature"), [
    ("normal", 5, (0.0, 0.2)),
    ("accurate", 10, 0.0),
    ("fast", 1, 0.0),
])
def test_the_preset_reaches_the_final_dictation_pass(
        preset, beam, temperature):
    engine, model = _engine(preset)

    engine.transcribe(_audio(), verbose=False)

    call = model.calls[-1]
    assert call["beam_size"] == beam
    assert call["temperature"] == temperature
    assert call["word_timestamps"] is True


def test_a_live_partial_keeps_its_own_greedy_pass():
    engine, model = _engine("accurate")

    engine.transcribe(_audio(), verbose=False, partial=True)

    call = model.calls[-1]
    assert call["beam_size"] == asr.PARTIAL_BEAM_SIZE
    assert call["temperature"] == asr.PARTIAL_TEMPERATURE


def test_the_preset_is_read_at_call_time_and_unknown_keeps_normal():
    engine, model = _engine("normal")
    engine.transcribe(_audio(), verbose=False)
    assert model.calls[-1]["beam_size"] == 5

    # Save updates the shared config dict; the next pass sees it, no restart.
    engine.cfg["quality_preset"] = "fast"
    engine.transcribe(_audio(), verbose=False)
    assert model.calls[-1]["beam_size"] == 1
    assert model.calls[-1]["temperature"] == 0.0

    engine.cfg["quality_preset"] = "turbo"
    engine.transcribe(_audio(), verbose=False)
    assert model.calls[-1]["beam_size"] == 5
    assert model.calls[-1]["temperature"] == (0.0, 0.2)


# -- the settings window ---------------------------------------------------
def test_the_preset_choice_lives_on_the_recording_tab():
    text = _source()
    record = _method(text, "_build_record_tab")
    for name in ("settings.g_quality", "settings.quality",
                 "settings.note_quality", "settings.quality_normal",
                 "settings.quality_accurate", "settings.quality_fast",
                 "self.v_quality"):
        assert name in record, name
    assert "self.v_quality" not in _method(text, "_build_model_tab")


def test_save_writes_the_preset_code():
    save = _method(_source(), "_save")
    assert 'c["quality_preset"] = self._quality_code()' in save


def test_reset_restores_the_default_preset():
    reset = _method(_source(), "_reset")
    assert "self.v_quality.set(" in reset
    assert 'd["quality_preset"]' in reset


def test_the_panel_exposes_no_raw_temperature():
    text = _source()
    assert "v_temperature" not in text
    assert "temperature" not in _method(text, "_save")


# -- the labels ------------------------------------------------------------
def test_every_new_key_is_localized():
    for lang in ("en", "ru"):
        catalog = _catalog(lang)
        missing = [key for key in NEW_KEYS
                   if not str(catalog.get(key, "")).strip()]
        assert not missing, (lang, missing)


def test_the_ru_labels_are_exact():
    catalog = _catalog("ru")
    assert catalog["settings.quality_normal"] == RU_NORMAL
    assert catalog["settings.quality_accurate"] == RU_ACCURATE
    assert catalog["settings.quality_fast"] == RU_FAST


def test_the_en_labels_are_exact():
    catalog = _catalog("en")
    assert catalog["settings.quality_normal"] == "Normal (recommended)"
    assert catalog["settings.quality_accurate"] == "Accurate"
    assert catalog["settings.quality_fast"] == "Fast"


def test_the_recording_tab_selects_the_stored_preset():
    if sys.platform != "win32":
        pytest.skip("settings_window uses Windows APIs")
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for a live Tk widget test")
    root.withdraw()
    try:
        import i18n
        import settings_window

        class Host(settings_window.SettingsWindow):
            """Only the Recording tab, built on a bare class shell."""

            def __init__(self, cfg):
                self.win = root
                self.cfg = cfg
                self.tr = lambda key, **kw: i18n.t(cfg, key, **kw)
                self._px = lambda value: int(round(value))
                self.rows = {}
                self._search_index = []
                self._frame_key = {}
                frame = tk.Frame(root)
                self._frame_key[frame] = "record"
                frame.pack()
                self._build_record_tab(frame)

        host = Host({"locale": "en", "language": "en",
                     "model": "large-v3-turbo"})
        assert list(host._quality_by_name.values()) == [
            "normal", "accurate", "fast"]
        assert host.v_quality.get() == "Normal (recommended)"
        assert host._quality_code() == "normal"
        host.v_quality.set(host.tr("settings.quality_accurate"))
        assert host._quality_code() == "accurate"
        assert host._quality_label("turbo") == "Normal (recommended)"
    finally:
        root.destroy()
