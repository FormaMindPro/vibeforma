"""Doctor: report shape, exit codes, read-only behaviour, no private data."""
import doctor


def test_render_lists_every_check_and_a_verdict():
    text = doctor.render(["header"], [
        (doctor.OK, "config", "readable"),
        (doctor.WARN, "model", "downloads on first start"),
    ])
    assert text.startswith("header")
    assert "[OK  ]" in text and "[WARN]" in text
    assert text.rstrip().endswith("RESULT: OK (1 warnings)")


def test_render_counts_failures():
    text = doctor.render([], [(doctor.FAIL, "microphone", "none")])
    assert "RESULT: FAIL (1 failed, 0 warnings)" in text


def test_exit_code_fails_only_on_a_failed_check():
    assert doctor.exit_code([(doctor.OK, "a", ""), (doctor.WARN, "b", "")]) == 0
    assert doctor.exit_code([(doctor.OK, "a", ""), (doctor.FAIL, "b", "")]) == 1


def test_report_carries_no_secrets():
    cfg = {
        "model": "small", "device": "cpu", "compute_type": "int8",
        "language": "en", "hotkey": "ctrl+alt+space",
        "http_api": True, "http_token": "TOKEN_SECRET_VALUE",
        "hotwords": "PRIVATE_TERM", "dictionary": [{"from": "PRIVATE_TERM",
                                                    "to": "x"}],
    }
    header, items = doctor.collect(cfg)
    text = doctor.render(header, items)
    assert "TOKEN_SECRET_VALUE" not in text
    assert "PRIVATE_TERM" not in text
    assert "token set (value hidden)" in text


def test_window_titles_and_dictation_text_never_appear():
    cfg = {"model": "small", "device": "cpu", "compute_type": "int8",
           "hotkey": "ctrl+alt+space", "log_window_titles": True,
           "history": True}
    header, items = doctor.collect(cfg)
    text = doctor.render(header, items)
    assert "window titles" in text
    assert "written to the log" in text
    # the report names the setting, never a title value
    assert "notepad" not in text.lower()


def test_doctor_creates_no_folders(monkeypatch, tmp_path):
    import config
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    monkeypatch.setattr(config, "LOG_DIR", str(data / "logs"))
    monkeypatch.setattr(config, "CONFIG_PATH", str(data / "config.json"))
    monkeypatch.setattr(config, "TEST_WAV", str(data / "test_ru.wav"))
    doctor.collect({"model": "small", "device": "cpu",
                    "compute_type": "int8", "hotkey": "ctrl+alt+space"})
    assert not data.exists()


def test_main_writes_the_report_file(tmp_path):
    report = tmp_path / "doctor.txt"
    code = doctor.main(str(report))
    assert code in (0, 1)
    text = report.read_text(encoding="utf-8")
    assert text.startswith("VibeForma doctor")
    assert "RESULT:" in text
    assert "privacy:" in text
