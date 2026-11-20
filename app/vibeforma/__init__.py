"""VibeForma service package.

The entry point stays ``app/main.py`` (the installer and the updater start
that file), while the code lives here: ``service`` (recording, recognition,
insertion), ``http_api``, ``updates``, ``selftest`` and ``app``.

Fully local: the network is used only for the first model download.
"""

STATE_LOADING = "loading"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"
STATE_PAUSED = "paused"
STATE_ERROR = "error"
STATE_UPDATING = "updating"

__all__ = [
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_LOADING",
    "STATE_PAUSED",
    "STATE_PROCESSING",
    "STATE_RECORDING",
    "STATE_UPDATING",
]
