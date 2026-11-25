"""Tests for tools/wer_cer.py: normalisation, metrics and the corpus manifest.

No model, GPU, network or recordings: the gate stays offline. The recorded
audio and the human's confirmation of each text are the card's verdict, not a
unit test.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "wer_cer.py"

_spec = importlib.util.spec_from_file_location("wer_cer", TOOL)
assert _spec is not None and _spec.loader is not None
wer_cer = importlib.util.module_from_spec(_spec)
sys.modules["wer_cer"] = wer_cer
_spec.loader.exec_module(wer_cer)


def _manifest() -> dict:
    return wer_cer.load_manifest(wer_cer.DEFAULT_MANIFEST)


def _speech(text: str) -> object:
    return wer_cer.Phrase("sample", "sample.wav", "en", "speech", (), text)


def test_normalize_ru_case_punctuation_and_yo():
    source = "\u0401\u0436\u0438\u043a, \u041c\u0418\u0420!"
    assert wer_cer.normalize(source, "ru") == "\u0435\u0436\u0438\u043a \u043c\u0438\u0440"


def test_normalize_en_case_and_punctuation():
    assert wer_cer.normalize("Hello, WORLD!", "en") == "hello world"


def test_numbers_are_compared_as_words():
    assert wer_cer.normalize("Order 47", "en") == wer_cer.normalize("Order forty seven", "en")
    assert wer_cer.normalize("\u041d\u043e\u043c\u0435\u0440 12", "ru") == wer_cer.normalize(
        "\u041d\u043e\u043c\u0435\u0440 \u0434\u0432\u0435\u043d\u0430\u0434\u0446\u0430\u0442\u044c",
        "ru",
    )


def test_digits_glued_to_letters_stay():
    assert wer_cer.normalize("large-v3-turbo", "en") == "large v3 turbo"


def test_wer_counts_substitution_deletion_insertion():
    phrase = _speech("a b c")
    substituted = wer_cer.measure(phrase, "a x c")
    assert (substituted.subs, substituted.dels, substituted.ins) == (1, 0, 0)
    assert substituted.wer == 1 / 3
    deleted = wer_cer.measure(phrase, "a c")
    assert (deleted.subs, deleted.dels, deleted.ins) == (0, 1, 0)
    inserted = wer_cer.measure(_speech("a b"), "a b c")
    assert (inserted.subs, inserted.dels, inserted.ins) == (0, 0, 1)


def test_cer_ignores_case_and_spaces():
    item = wer_cer.measure(_speech("Ab cd"), "ab xd")
    assert item.cer == 1 / 4


def test_non_speech_output_is_a_false_positive():
    silence = wer_cer.Phrase("silence-01", "silence-01.wav", "", "silence", ("silence",), "")
    clean = wer_cer.measure(silence, "")
    assert clean.false_positive is False
    assert clean.wer == 0.0
    noisy = wer_cer.measure(silence, "hello there")
    assert noisy.false_positive is True
    assert noisy.wer == 1.0


def test_manifest_has_24_phrases_and_every_section():
    payload = _manifest()
    phrases = wer_cer.parse_phrases(payload)
    assert len(phrases) == 24
    assert wer_cer.validate_manifest(payload) == []
    assert len({phrase.id for phrase in phrases}) == 24
    assert len({phrase.file for phrase in phrases}) == 24
    speech = [phrase for phrase in phrases if phrase.kind == "speech"]
    assert len(speech) == 20
    assert {phrase.language for phrase in speech} == {"ru", "en"}
    kinds = {phrase.kind for phrase in phrases}
    assert {"noise", "music", "silence"} <= kinds
    tags = {tag for phrase in phrases for tag in phrase.tags}
    assert {"names", "numbers", "terms", "noise", "music", "silence"} <= tags


def test_manifest_file_names_match_ids():
    for phrase in wer_cer.parse_phrases(_manifest()):
        assert Path(phrase.file).stem == phrase.id


def test_baseline_block_points_at_the_repository_fixture():
    baseline = wer_cer.parse_baseline(_manifest())
    assert baseline["language"] == "ru"
    assert baseline["text"].strip()
    path = (wer_cer.DEFAULT_MANIFEST.parent / baseline["file"]).resolve()
    assert path.name == "test_ru.wav"
    assert path.is_file()


def test_report_has_a_line_per_file_and_a_speech_total():
    phrase = wer_cer.parse_phrases(_manifest())[0]
    item = wer_cer.measure(phrase, phrase.text)
    report = wer_cer.render_report([item], wer_cer.DEFAULT_MANIFEST)
    assert phrase.id in report
    assert "TOTAL speech" in report
    assert "WER 0.00%" in report


def test_missing_recording_is_named():
    phrase = wer_cer.parse_phrases(_manifest())[0]
    report = wer_cer.render_report(
        [wer_cer.Measure(phrase=phrase, status="missing")],
        wer_cer.DEFAULT_MANIFEST,
    )
    assert f"MISSING {phrase.file}" in report
    assert "missing recordings" in report


def test_hypothesis_dir_prefers_the_file_stem(tmp_path):
    phrase = wer_cer.Phrase("custom-id", "ru-01.wav", "ru", "speech", (), "")
    (tmp_path / "ru-01.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "custom-id.txt").write_text("other", encoding="utf-8")
    assert wer_cer._read_hypothesis(tmp_path, phrase) == "hello"


def test_validation_rejects_a_short_corpus():
    payload = _manifest()
    payload["phrases"] = payload["phrases"][:3]
    problems = wer_cer.validate_manifest(payload)
    assert any("expected 24" in problem for problem in problems)


def test_self_test_passes_without_recordings():
    assert wer_cer.self_test() == 0


def test_cli_self_test_exits_zero():
    assert wer_cer.main(["--self-test"]) == 0
