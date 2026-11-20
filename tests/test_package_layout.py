"""T10 layout: the service package, the main shim, the installer split, locks."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_main_is_a_small_shim():
    text = read(os.path.join(APP, "main.py"))
    assert len(text.splitlines()) < 300
    assert "def main(" not in text          # the flow lives in vibeforma
    assert '"--selftest"' in text         # the release check looks for it


def test_public_entry_points_stay_importable():
    import main

    for name in ("Service", "SingleInstance", "main", "selftest", "beep",
                 "start_http_api", "start_update_checker", "setup_logging",
                 "LOG_DIR", "SELFTEST_FLAG"):
        assert hasattr(main, name), name
    assert main.SELFTEST_FLAG == "--selftest"


def test_package_split_keeps_the_service_surface():
    from vibeforma import STATE_ERROR, STATE_IDLE, STATE_RECORDING
    from vibeforma.http_api import HTTP_MAX_BODY, start_http_api
    from vibeforma.service import Service

    assert len({STATE_IDLE, STATE_RECORDING, STATE_ERROR}) == 3
    assert callable(start_http_api) and callable(Service)
    assert HTTP_MAX_BODY > 0


def test_installer_is_a_thin_entry_over_modules():
    import install

    text = read(os.path.join(ROOT, "install.py"))
    assert len(text.splitlines()) < 400   # flow + re-exports, not the steps
    for name in ("install_core", "install_env", "install_steps",
                 "install_system"):
        assert os.path.isfile(os.path.join(ROOT, name + ".py")), name
    # tests patch module objects and a few names on `install`
    assert install.subprocess is not None
    assert install.shutil is not None
    assert callable(install.run) and callable(install.main)
    assert install.requirements_file("cuda") == "requirements-cuda.txt"
    assert install.requirements_file("cpu") == "requirements.txt"


def lock_entries(path):
    """Yield (line, has_hash) for every requirement entry in a lock file."""
    lines = read(path).splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        has_hash = False
        while index < len(lines) and lines[index].strip().startswith("--hash="):
            has_hash = True
            index += 1
        while index < len(lines) and lines[index].strip().startswith("#"):
            index += 1
        yield line, has_hash


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-cuda.txt"])
def test_dependency_locks_pin_every_package_with_hashes(name):
    path = os.path.join(APP, name)
    entries = list(lock_entries(path))
    assert entries, name
    for line, has_hash in entries:
        assert re.match(r"^[A-Za-z0-9_.\-]+==[^ ;]+", line), line
        assert has_hash, line


def test_cuda_lock_carries_the_cuda_wheels_and_inputs_exist():
    cuda = read(os.path.join(APP, "requirements-cuda.txt"))
    assert "nvidia-cublas-cu12==" in cuda
    assert "nvidia-cudnn-cu12==" in cuda
    assert os.path.isfile(os.path.join(APP, "requirements.in"))
    assert os.path.isfile(os.path.join(APP, "requirements-cuda.in"))
