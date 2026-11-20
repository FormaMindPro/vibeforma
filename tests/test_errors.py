"""Error catalog: stable codes, localized text, a next action, no raw text."""
import json
import os

import errors

HERE = os.path.dirname(os.path.abspath(__file__))
LOCALES = os.path.join(HERE, "..", "app", "locales")


def cfg(locale):
    return {"locale": locale}


def locale_keys(code):
    with open(os.path.join(LOCALES, code + ".json"), encoding="utf-8") as f:
        return json.load(f)


def test_permission_error_is_localized_with_action():
    message = errors.text(cfg("en"), "mic",
                          PermissionError("Access is denied"))
    assert "Windows did not allow access" in message
    assert "Settings > Privacy" in message
    assert "Access is denied" not in message


def test_russian_microphone_error_is_localized_with_action():
    message = errors.text(cfg("ru"), "mic",
                          PermissionError("Access is denied"))
    assert "Windows не разрешила" in message
    assert "Конфиденциальность" in message


def test_busy_microphone_has_its_own_code():
    assert errors.code_for("mic", RuntimeError("device busy")) == "mic.busy"
    assert "занят" in errors.text(cfg("ru"), "mic",
                                  RuntimeError("device busy"))


def test_model_errors_are_classified():
    assert errors.code_for("model", RuntimeError("CUDA out of memory")) \
        == "model.memory"
    assert errors.code_for("model", OSError("Connection reset by peer")) \
        == "model.network"
    assert errors.code_for("model", RuntimeError("weird")) == "model.load"


def test_unknown_kind_falls_back_to_recognition():
    assert errors.code_for("strange", RuntimeError("x")) \
        == "recognition.failed"


def test_raw_exception_text_never_reaches_the_user():
    samples = [
        PermissionError("Access is denied"),
        RuntimeError("CUDA out of memory at 0x7ff"),
        OSError("[WinError 10054] Connection reset"),
        RuntimeError("internal traceback line 42"),
    ]
    for exc in samples:
        for kind in ("mic", "model", "recognition", "file"):
            message = errors.text(cfg("ru"), kind, exc)
            assert str(exc) not in message
            assert type(exc).__name__ not in message


def test_last_line_has_no_action_and_no_newline():
    line = errors.last_line(cfg("en"), "recognition", RuntimeError("x"))
    assert "\n" not in line
    assert line == "The recording could not be recognized."


def test_every_code_is_translated_in_both_locales():
    codes = set(errors.FALLBACK.values()) | {
        "mic.permission", "mic.busy", "model.memory", "model.network"}
    for code in codes:
        for locale in ("en", "ru"):
            strings = locale_keys(locale)
            for part in ("text", "action"):
                key = f"errors.{code}.{part}"
                assert key in strings, key
                assert strings[key].strip(), key
