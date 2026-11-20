"""Test-only Ed25519 signer and minisign record builders.

Verification correctness is anchored by an RFC 8032 vector in the tests;
this helper only produces fixtures for the integration path.
"""
import base64
import hashlib

import release_verify as rv

KEY_ID = b"VFtest01"


def keypair(seed=None):
    seed = bytes(range(32)) if seed is None else seed
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    public = rv._encode(rv._mul(rv._BASE, a))
    payload = b"Ed" + KEY_ID + public
    return seed, base64.b64encode(payload).decode("ascii"), public


def sign(seed, message, prehashed=False):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:]
    public = rv._encode(rv._mul(rv._BASE, a))
    body = hashlib.blake2b(message, digest_size=64).digest() if prehashed else message
    r = rv._h(prefix + body) % rv._L
    big_r = rv._encode(rv._mul(rv._BASE, r))
    k = rv._h(big_r + public + body) % rv._L
    s = (r + k * a) % rv._L
    algorithm = b"ED" if prehashed else b"Ed"
    record = algorithm + KEY_ID + big_r + s.to_bytes(32, "little")
    return base64.b64encode(record).decode("ascii")


def signature_file(seed, message, prehashed=False):
    record = sign(seed, message, prehashed)
    return (
        "untrusted comment: test signature\n"
        + record + "\n"
        + "trusted comment: test\n"
        + base64.b64encode(b"\x00" * 64).decode("ascii") + "\n"
    )
