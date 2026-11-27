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


# Russian strings for errors.model.driver, as \\u escapes to keep the
# test sources ASCII (audit-02 #10).
RU_DRIVER_TEXT = ("\u041c\u043e\u0434\u0435\u043b\u044c \u043d\u0435 "
                  "\u0437\u0430\u0433\u0440\u0443\u0437\u0438\u043b\u0430\u0441\u044c: "
                  "\u043f\u0440\u043e\u0431\u043b\u0435\u043c\u0430 CUDA-"
                  "\u0434\u0440\u0430\u0439\u0432\u0435\u0440\u0430.")
RU_DRIVER_ACTION = ("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 "
                    "\u0434\u0440\u0430\u0439\u0432\u0435\u0440 NVIDIA "
                    "\u0438\u043b\u0438 \u043f\u0435\u0440\u0435\u043a\u043b"
                    "\u044e\u0447\u0438\u0442\u0435 "
                    "\u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432"
                    "\u043e \u043d\u0430 CPU \u0432 "
                    "\u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a"
                    "\u0430\u0445.")


def test_cuda_driver_errors_get_their_own_code_sharp_edge():
    """A bare CUDA mention is no longer an out-of-memory claim (audit-02 #10)."""
    assert errors.code_for("model",
                           RuntimeError("CUDA driver mismatch")) \
        == "model.driver"
    assert errors.code_for("model",
                           RuntimeError("Could not load cudnn DLL")) \
        == "model.driver"
    assert errors.code_for("model",
                           RuntimeError("CUDA out of memory")) \
        == "model.memory"


def test_model_driver_resolves_in_both_locales_sharp_edge():
    exc = RuntimeError("CUDA driver mismatch")
    en = errors.text(cfg("en"), "model", exc)
    assert en == ("The model failed to load: a CUDA driver problem.\n"
                  "Update your NVIDIA driver, or switch the device to "
                  "CPU in settings.")
    ru = errors.text(cfg("ru"), "model", exc)
    assert ru == RU_DRIVER_TEXT + "\n" + RU_DRIVER_ACTION
    strings = locale_keys("ru")
    assert strings["errors.model.driver.text"] == RU_DRIVER_TEXT
    assert strings["errors.model.driver.action"] == RU_DRIVER_ACTION
