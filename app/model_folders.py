"""Local CT2 model folders: the stored list, its validity, safe removal.

The Model tab keeps a short list of model folders beside the single ``model``
value. Only the record is stored in ``config.json`` under ``model_folders``:
a name and an absolute path, nothing else. Validity is computed when the list
is drawn, so a folder that lost its ``model.bin`` is marked instead of keeping
a stale "ready".

A folder in this list belongs to the person. Switching away from it never
deletes it -- even with ``model_keep_old=False``, which removes only the models
VibeForma downloaded into ``MODELS_DIR``.
"""
from __future__ import annotations

import os

MAX_MODEL_FOLDERS = 20
MAX_FOLDER_NAME = 80


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def absolute(path) -> str:
    """The folder path as stored: expanded, absolute, no trailing separator."""
    raw = _text(path)
    if not raw:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    return os.path.abspath(expanded) if expanded else ""


def same_path(left, right) -> bool:
    """True when two spellings name the same folder on this platform."""
    first, second = absolute(left), absolute(right)
    if not first or not second:
        return False
    return (os.path.normcase(os.path.normpath(first))
            == os.path.normcase(os.path.normpath(second)))


def name_for(path) -> str:
    """A short folder name for the list; the path itself when it has none."""
    folder = absolute(path)
    name = os.path.basename(folder.rstrip("\\/"))
    return (name or folder)[:MAX_FOLDER_NAME]


def normalize(entries) -> list[dict]:
    """The stored list as records of ``name`` and an absolute ``path``.

    A malformed entry, a record without a path and a repeated folder are
    dropped; the list is capped. Nothing here touches the disk: a folder that
    no longer exists is still a record, and the list shows it as invalid.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        folder = absolute(item.get("path"))
        if not folder:
            continue
        key = os.path.normcase(os.path.normpath(folder))
        if key in seen:
            continue
        seen.add(key)
        name = _text(item.get("name")) or name_for(folder)
        out.append({"name": name[:MAX_FOLDER_NAME], "path": folder})
        if len(out) >= MAX_MODEL_FOLDERS:
            break
    return out


def folder_problem(path) -> str | None:
    """Why the folder cannot be offered as a model, or None.

    The list calls this whenever it is drawn, so a folder that lost its
    ``model.bin`` turns invalid instead of keeping an old mark. The deep
    check of the weights stays in ``asr._model_folder_problem`` and runs when
    the model is actually loaded; this is the one the tab names.
    """
    folder = absolute(path)
    if not folder or not os.path.isdir(folder):
        return "not_a_folder"
    if not os.path.isfile(os.path.join(folder, "model.bin")):
        return "missing_model_bin"
    return None


def folder_of(value) -> str | None:
    """The folder a ``model`` value names, or None for a catalog name/repo id.

    ``asr._model_path`` stays the one place that tells a folder from a name
    (card-244); it is imported here so opening the settings window never pulls
    the engine and numpy in.
    """
    import asr
    return asr._model_path(value)


def is_current(model, path) -> bool:
    """True when the live ``model`` value names exactly this folder."""
    current = folder_of(model)
    return current is not None and same_path(current, path)


def _resolved(path: str) -> str:
    """The absolute path with links resolved and case folded, for comparing."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _strictly_inside(root: str, target: str) -> bool:
    """True when the resolved ``target`` sits below the resolved ``root``."""
    if target == root:
        return False
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:  # a different drive on Windows
        return False


def removal_target(old, models_dir) -> str | None:
    """The downloaded model folder to delete for ``old``, or None.

    Only a value that is not a folder -- a catalog name or a repo id -- can
    point at a copy VibeForma downloaded under ``models_dir``. A folder value
    is the person's own model: ``model_keep_old=False`` forgets the old model,
    it does not delete their files.

    The deletion is confined to ``models_dir``: the candidate is resolved
    (``..`` segments, symlinks, junctions) and must stay strictly below the
    resolved ``models_dir``. A value that escapes -- dot-dot traversal, an
    absolute or drive-relative path, a UNC spelling, a link pointing out --
    returns None, and so does ``models_dir`` itself. Anything doubtful fails
    closed; the classifier above is not trusted on its own (card-266).
    """
    name = _text(old)
    if not name:
        return None
    if folder_of(name) is not None:
        return None
    # A non-folder value is a relative catalog name. An absolute, drive or
    # UNC spelling reaching here means the classifier let a path through:
    # never a deletion target, whatever the rest of the path says.
    drive, _tail = os.path.splitdrive(name)
    if drive or os.path.isabs(name):
        return None
    root_dir = absolute(str(models_dir or ""))
    if not root_dir:
        return None
    candidate = absolute(os.path.join(root_dir, name))
    if not candidate:
        return None
    try:
        root, target = _resolved(root_dir), _resolved(candidate)
    except OSError:
        return None
    if not _strictly_inside(root, target):
        return None
    return candidate
