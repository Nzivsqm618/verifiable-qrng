"""Ed25519 signatures over ``pool_hash``.

Verification is a pure-Python port of the RFC 8032 section 6 reference code,
so the offline verifier needs only the standard library. It handles public
data only, so it does not need to be constant-time. Signing touches the
private key and uses the ``cryptography`` package (``pip install vqrng[sign]``).

A signature proves who produced the evidence only when the verifier already
trusts the public key. A key read from the payload itself proves nothing:
anyone can generate a key pair and sign a forged record.
"""

from __future__ import annotations

import hashlib
import secrets

SIGNATURE_CONTEXT = b"vqrng-evidence-v3:"

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_G_Y = 4 * pow(5, _P - 2, _P) % _P


def signed_message(pool_hash: str) -> bytes:
    return SIGNATURE_CONTEXT + pool_hash.encode("utf-8")


def _point_add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _point_mul(scalar: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(encoded: bytes) -> tuple[int, int, int, int] | None:
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return x, y, 1, x * y % _P


_G = _decompress(_G_Y.to_bytes(32, "little"))


def _from_hex(value: object, length: int) -> bytes | None:
    if not isinstance(value, str) or len(value) != 2 * length:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


def verify_signature(public_key: object, signature: object, pool_hash: str) -> bool:
    """Check a hex Ed25519 ``signature`` by hex ``public_key`` over ``pool_hash``."""
    key, sig = _from_hex(public_key, 32), _from_hex(signature, 64)
    if key is None or sig is None:
        return False
    a, r = _decompress(key), _decompress(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + key + signed_message(pool_hash)).digest(), "little") % _L
    return _point_equal(_point_mul(s, _G), _point_add(r, _point_mul(h, a)))  # type: ignore[arg-type]


def _private_key(private_key: str):  # type: ignore[no-untyped-def]
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise RuntimeError("Signing needs the 'cryptography' package: pip install vqrng[sign]") from exc
    seed = _from_hex(private_key.strip(), 32)
    if seed is None:
        raise ValueError("The signing key must be 64 hex characters (a 32-byte Ed25519 seed).")
    return Ed25519PrivateKey.from_private_bytes(seed)


def generate_signing_key() -> str:
    """Return a new random Ed25519 private key seed as hex. Keep it secret."""
    return secrets.token_hex(32)


def public_key_hex(private_key: str) -> str:
    """Return the hex public key for a hex private key seed, to share with verifiers."""
    key = _private_key(private_key)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def sign_pool_hash(private_key: str, pool_hash: str) -> tuple[str, str]:
    """Return ``(signature, public_key)`` as hex for ``pool_hash``."""
    key = _private_key(private_key)
    return key.sign(signed_message(pool_hash)).hex(), public_key_hex(private_key)
