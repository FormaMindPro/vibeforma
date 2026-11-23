"""Loopback capture of the default output through ``soundcard``.

PortAudio has no loopback in the bundled build, so this is the only module
that knows ``soundcard``: Core Audio is opened directly and the rest of the
audio path (Recorder, Resampler, VAD, ASR) stays unchanged. The import is
optional so a machine without the package keeps a working microphone.
"""
try:
    import soundcard as sc
except Exception:  # noqa: BLE001 - the microphone path must still work
    sc = None


class SystemAudioUnavailable(RuntimeError):
    pass


def default_loopback():
    """(microphone, output name) for the default output, or raise."""
    if sc is None:
        raise SystemAudioUnavailable("soundcard is not installed")
    try:
        speaker = sc.default_speaker()
    except Exception as exc:
        raise SystemAudioUnavailable(f"no default output: {exc}") from exc
    if speaker is None:
        raise SystemAudioUnavailable("no default output device")
    try:
        mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    except Exception as exc:
        raise SystemAudioUnavailable(
            f"no loopback for {speaker.name}: {exc}") from exc
    if mic is None:
        raise SystemAudioUnavailable(f"no loopback for {speaker.name}")
    return mic, str(speaker.name)
