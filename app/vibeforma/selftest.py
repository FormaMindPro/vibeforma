"""Machine gate: imports -> model -> test WAV -> text; exit 0/1."""
from __future__ import annotations

import os
import time

from config import SERVICE_DIR, TEST_WAV, VERSION, load

from .util import setup_logging


def selftest() -> int:
    log = setup_logging(console=True)
    log.info("=== selftest started ===")
    print("VibeForma", VERSION, "service dir:", SERVICE_DIR)
    try:
        # (a) imports
        import ctranslate2
        import faster_whisper
        import numpy as np
        import pyperclip
        import sounddevice
        print(f"imports_ok: faster_whisper={faster_whisper.__version__} "
              f"ctranslate2={ctranslate2.__version__} "
              f"sounddevice={getattr(sounddevice, '__version__', '?')}")
        print("cuda_devices:", ctranslate2.get_cuda_device_count())

        cfg = load()
        print("config:", {k: cfg[k] for k in ("model", "device", "compute_type",
                                              "language", "locale", "hotkey",
                                              "input_device")})
        cfg = dict(cfg)
        cfg["language"] = "ru"   # selftest WAV is Russian; pin it for the gate
        print("selftest_language: ru (pinned for the Russian test WAV)")

        # (b) model
        import asr as asr_mod
        engine = asr_mod.ASR(cfg, log)
        t0 = time.time()
        engine.load()
        load_s = time.time() - t0
        print(f"model_load_s: {load_s:.1f}")

        # (c) test WAV -> text (language auto = per-phrase detection)
        import audio as audio_mod
        if not os.path.exists(TEST_WAV):
            raise RuntimeError(f"test WAV not found: {TEST_WAV}")
        audio = audio_mod.load_wav_16k(TEST_WAV)
        print(f"test_wav_s: {audio.shape[0] / 16000.0:.1f}")
        engine.transcribe(np.zeros((16000,), dtype=np.float32))  # warm-up
        t0 = time.time()
        text = engine.transcribe(audio)
        dt = time.time() - t0
        print(f"transcribe_s: {dt:.2f}")
        print("text:", text)
        has_cyr = any("\u0430" <= ch <= "\u044f" or "\u0410" <= ch <= "\u042f"
                      for ch in text)
        # test phrase, speech, recognition, local (the Russian test WAV)
        roots = ("\u0442\u0435\u0441\u0442", "\u0444\u0440\u0430\u0437",
                 "\u0440\u0435\u0447", "\u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u0432",
                 "\u043b\u043e\u043a\u0430\u043b\u044c\u043d")
        hit = sum(1 for r in roots if r in text.lower())
        print("cyrillic:", has_cyr, "keyword_hits:", hit)

        # (d) clipboard round-trip (non-destructive)
        prev = None
        try:
            prev = pyperclip.paste()
        except Exception as e:  # noqa: BLE001 - the gate reports a broken clipboard, it does not raise
            log.debug("Clipboard read: %s", e)
        pyperclip.copy("VF_SELFTEST_MARKER")
        ok_clip = pyperclip.paste() == "VF_SELFTEST_MARKER"
        if prev is not None:
            pyperclip.copy(prev)
        print("clipboard_roundtrip:", ok_clip)

        gate = has_cyr and hit >= 1 and ok_clip
        print("SELFTEST OK" if gate else "SELFTEST FAIL: keyword/cyrillic/clipboard")
        log.info("=== selftest finished: %s ===", "OK" if gate else "FAIL")
        return 0 if gate else 1
    except Exception as e:
        print(f"SELFTEST FAIL: {e}")
        log.exception("selftest crashed: %s", e)
        return 1
