"""Evidence payload format shared by generation and offline verification.

Standard library only, so the verifier can use it without Qiskit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

EVIDENCE_VERSION = "4"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUSES = (STATUS_COMPLETED, STATUS_PARTIAL)

# A "pool" record carries the numbers themselves. A "seed" record is a pool
# of SEED_BYTES values over [0, 255], whose bytes seed a local QSEED_EXPANDER.
KIND_POOL = "pool"
KIND_SEED = "seed"
KINDS = (KIND_POOL, KIND_SEED)
SEED_BYTES = 32
QSEED_EXPANDER = "numpy.random.PCG64"

# Added after pool_hash is computed, so the hash cannot cover them.
UNHASHED_FIELDS = ("pool_hash", "signature", "public_key")

# Measurement settings in the order the CHSH sum uses them, and the
# two-bit outcomes of each, Alice's bit first.
CHSH_SETTINGS = ("A0B0", "A0B1", "A1B0", "A1B1")
CHSH_OUTCOMES = ("00", "01", "10", "11")
CHSH_CLASSICAL_BOUND = 2.0
CHSH_MAX_GAP_SECONDS = 10.0


def resolve_range(
    min_val: int | None, max_val: int | None, digits: int | None, pad: bool
) -> tuple[int, int]:
    """Validate the requested bounds and return the inclusive ``(min, max)`` range."""
    if digits is not None:
        if min_val is not None or max_val is not None:
            raise ValueError("Provide either min_val/max_val or digits, not both.")
        if digits < 1:
            raise ValueError("digits must be >= 1.")
        low = 0 if pad else 10 ** (digits - 1)
        return low, 10**digits - 1

    if pad:
        raise ValueError("pad requires digits.")
    if min_val is None or max_val is None:
        raise ValueError("Provide both min_val and max_val, or digits.")
    if min_val > max_val:
        raise ValueError(f"min_val ({min_val}) must be <= max_val ({max_val}).")
    return min_val, max_val


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_hash(evidence: dict) -> str:
    """Hash every evidence field except ``pool_hash``, ``signature``, and ``public_key``."""
    return sha256_hex(canonical_json({k: v for k, v in evidence.items() if k not in UNHASHED_FIELDS}))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp that carries a UTC offset, or return ``None``.

    Fractional seconds are padded to six digits first. Python 3.10's
    ``fromisoformat`` only accepts the widths ``isoformat`` itself emits,
    while IBM reports widths such as ``.5``.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    dot = text.find(".")
    if dot != -1:
        end = dot + 1
        while end < len(text) and text[end].isdigit():
            end += 1
        text = text[:dot] + "." + (text[dot + 1:end] + "000000")[:6] + text[end:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
