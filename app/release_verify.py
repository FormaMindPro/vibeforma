"""Release integrity: minisign-compatible Ed25519 check, manifest, safe zip.

The update path must authenticate the publisher before any installer code
runs. The public key is compiled into the app (RELEASE_PUBLIC_KEY); when it
is empty every update is refused (fail-closed) until a maintainer embeds the
production key. Hashing alone is not enough: a compromised release page
could replace both the zip and its checksums.

Ed25519 verification is implemented here in pure Python (RFC 8032) so the
runtime needs no extra dependency. Minisign signatures over the raw manifest
("Ed") and over its BLAKE2b-512 digest ("ED") are both accepted.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import zipfile

# Base64 of: b"Ed" + 8-byte key id + 32-byte public key.
# Empty means "no trust anchor configured" and disables updates.
RELEASE_PUBLIC_KEY = ""

MAX_MEMBERS = 2000
MAX_UNCOMPRESSED = 300 * 1024 * 1024
REQUIRED_FILES = (
    "install.py",
    "app/main.py",
    "app/config.py",
    "app/run.cmd",
    "test_ru.wav",
)

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseError(Exception):
    """A release failed verification; the message is safe to log."""


def _inv(x):
    return pow(x, _P - 2, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_BX = _xrecover(_BY)
_BASE = (_BX, _BY, 1, _BX * _BY % _P)


def _add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * 2 * _D * t2 % _P
    d = z1 * 2 * z2 % _P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(point, n):
    if n == 0:
        return (0, 1, 1, 0)
    q = _mul(point, n // 2)
    q = _add(q, q)
    if n & 1:
        q = _add(q, point)
    return q


def _encode(point):
    x, y, z, _t = point
    zi = _inv(z)
    x = x * zi % _P
    y = y * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(raw):
    if len(raw) != 32:
        raise ReleaseError("invalid Ed25519 point")
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (raw[31] >> 7):
        x = _P - x
    return (x, y, 1, x * y % _P)


def _h(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def ed25519_verify(public_key, message, signature):
    """True only for a valid 32-byte key / 64-byte signature pair."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        r = _decode(signature[:32])
        a = _decode(public_key)
    except ReleaseError:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = _h(signature[:32] + public_key + message) % _L
    return _encode(_mul(_BASE, s)) == _encode(_add(r, _mul(a, k)))


def parse_public_key(text):
    """Decode a minisign public key payload: id + 32-byte Ed25519 key."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ReleaseError("release public key is not valid base64") from None
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise ReleaseError("release public key has an unexpected format")
    return raw[2:10], raw[10:42]


def parse_signature(text):
    """First signature record from a minisign .minisig file."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("untrusted comment:"):
            continue
        try:
            raw = base64.b64decode(line, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 74 and raw[:2] in (b"Ed", b"ED"):
            return raw[:2], raw[2:10], raw[10:]
    raise ReleaseError("release signature file has no signature record")


def verify_manifest(manifest_bytes, signature_text, public_key_b64=None):
    """Verify a detached minisign signature over the manifest bytes."""
    key_text = RELEASE_PUBLIC_KEY if public_key_b64 is None else public_key_b64
    if not key_text:
        raise ReleaseError(
            "release signing key is not configured; update refused")
    key_id, public_key = parse_public_key(key_text)
    algorithm, sig_key_id, signature = parse_signature(signature_text)
    if sig_key_id != key_id:
        raise ReleaseError("release signature was made by a different key")
    message = manifest_bytes
    if algorithm == b"ED":
        message = hashlib.blake2b(manifest_bytes, digest_size=64).digest()
    if not ed25519_verify(public_key, message, signature):
        raise ReleaseError("release signature is invalid")
    return True


def parse_manifest(manifest_bytes):
    """Parse and validate the signed release manifest."""
    try:
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ReleaseError("release manifest is not valid UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise ReleaseError("release manifest must be an object")
    version = data.get("version")
    zip_name = data.get("zip")
    digest = data.get("sha256")
    files = data.get("files")
    if not isinstance(version, str) or not version:
        raise ReleaseError("release manifest has no version")
    if not isinstance(zip_name, str) or not zip_name:
        raise ReleaseError("release manifest has no zip name")
    if not isinstance(digest, str) or not _SHA256_RE.match(digest.lower()):
        raise ReleaseError("release manifest has an invalid sha256")
    if not isinstance(files, list) or not files:
        raise ReleaseError("release manifest has no file list")
    cleaned = []
    for name in files:
        if not isinstance(name, str):
            raise ReleaseError("release manifest file list is invalid")
        cleaned.append(_check_member_name(name))
    if len(set(cleaned)) != len(cleaned):
        raise ReleaseError("release manifest lists a file twice")
    return {
        "version": version.lstrip("vV"),
        "zip": os.path.basename(zip_name),
        "sha256": digest.lower(),
        "files": cleaned,
    }


def _check_member_name(name):
    if not name or "\x00" in name:
        raise ReleaseError("release archive has an empty or broken path")
    if "\\" in name:
        raise ReleaseError("release archive uses a backslash in a path")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ReleaseError("release archive contains an absolute path")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReleaseError("release archive contains an unsafe path")
    return name


def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_zip(zip_path, manifest):
    """Check the zip hash and every member before extraction."""
    actual = sha256_file(zip_path)
    if actual != manifest["sha256"]:
        raise ReleaseError(
            "release zip hash does not match the signed manifest")
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if not infos:
            raise ReleaseError("release archive is empty")
        if len(infos) > MAX_MEMBERS:
            raise ReleaseError("release archive has too many entries")
        names = []
        total = 0
        for info in infos:
            name = _check_member_name(info.filename)
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ReleaseError("release archive contains a symbolic link")
            if info.is_dir():
                continue
            if name in names:
                raise ReleaseError("release archive contains a duplicate entry")
            names.append(name)
            total += info.file_size
        if total > MAX_UNCOMPRESSED:
            raise ReleaseError("release archive is too large when unpacked")
        allowed = set(manifest["files"])
        extra = sorted(set(names) - allowed)
        if extra:
            raise ReleaseError(
                "release archive contains files not in the manifest: "
                + ", ".join(extra[:3]))
        missing = sorted(allowed - set(names))
        if missing:
            raise ReleaseError(
                "release archive is missing manifest files: "
                + ", ".join(missing[:3]))
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1:
            raise ReleaseError(
                "release archive must unpack into exactly one folder")
        root = roots.pop()
        required = {root + "/" + rel for rel in REQUIRED_FILES}
        absent = sorted(required - set(names))
        if absent:
            raise ReleaseError(
                "release archive is missing required files: "
                + ", ".join(absent[:3]))
    return root


def extract_verified(zip_path, manifest, dest):
    """Extract only verified members; return the folder holding install.py."""
    root = verify_zip(zip_path, manifest)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = _check_member_name(info.filename)
            if name not in manifest["files"]:
                raise ReleaseError("release archive changed during extraction")
            archive.extract(info, dest)
    src = os.path.join(dest, root)
    if not os.path.isfile(os.path.join(src, "install.py")):
        raise ReleaseError("install.py is not at the release root")
    return src


def version_matches(manifest, tag):
    """The signed manifest must describe the release we are installing."""
    expected = str(tag).lstrip("vV")
    if manifest["version"] != expected:
        raise ReleaseError(
            f"release manifest version {manifest['version']} "
            f"does not match {expected}")
    return True
