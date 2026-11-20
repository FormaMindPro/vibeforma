"""VibeForma entry point (the file the installer and updater start).

The service itself lives in the ``vibeforma`` package; this file keeps the
public names that tests, the installer and the updater already use.
"""
from config import LOG_DIR
from vibeforma import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_UPDATING,
)
from vibeforma.app import main
from vibeforma.http_api import HTTP_MAX_BODY, HTTP_MIN_INTERVAL_S, HTTP_TOKEN_HEADER, start_http_api
from vibeforma.selftest import selftest
from vibeforma.service import Service
from vibeforma.updates import start_update_checker
from vibeforma.util import SingleInstance, beep, ensure_autostart, setup_logging

# main.py --selftest runs the machine gate (vibeforma/selftest.py)
SELFTEST_FLAG = "--selftest"

__all__ = [
    "HTTP_MAX_BODY",
    "HTTP_MIN_INTERVAL_S",
    "HTTP_TOKEN_HEADER",
    "LOG_DIR",
    "SELFTEST_FLAG",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_LOADING",
    "STATE_PAUSED",
    "STATE_PROCESSING",
    "STATE_RECORDING",
    "STATE_UPDATING",
    "Service",
    "SingleInstance",
    "beep",
    "ensure_autostart",
    "main",
    "selftest",
    "setup_logging",
    "start_http_api",
    "start_update_checker",
]

if __name__ == "__main__":
    main()
