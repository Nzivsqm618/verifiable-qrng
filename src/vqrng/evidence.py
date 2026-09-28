"""Evidence payload format shared by generation and offline verification.

Standard library only, so the verifier can use it without Qiskit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

EVIDENCE_VERSION = "2"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUSES = (STATUS_COMPLETED, STATUS_PARTIAL)

# Measurement settings in the order the CHSH sum uses them, and the
# two-bit outcomes of each, Alice's bit first.
CHSH_SETTINGS = ("A0B0", "A0B1", "A1B0", "A1B1")
CHSH_OUTCOMES = ("00", "01", "10", "11")
CHSH_CLASSICAL_BOUND = 2.0


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
    """Hash every evidence field except ``pool_hash`` itself."""
    return sha256_hex(canonical_json({k: v for k, v in evidence.items() if k != "pool_hash"}))
