"""Offline verification of vqrng evidence payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from vqrng.verifier.level_a import verify_level_a


@dataclass(frozen=True)
class VerificationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)


def verify(evidence: dict | str) -> VerificationResult:
    """Verify an evidence dictionary or its JSON string."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError as exc:
            return VerificationResult(False, [f"Evidence is not valid JSON: {exc}"])
    is_valid, errors = verify_level_a(evidence)
    return VerificationResult(is_valid, errors)


__all__ = ["VerificationResult", "verify", "verify_level_a"]
