"""Offline verification of vqrng evidence payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from vqrng.verifier.level_a import verify_level_a
from vqrng.verifier.level_b import verify_level_b
from vqrng.verifier.level_c import verify_level_c

LEVEL_NAMES = {
    "A": "reproducible conversion",
    "B": "tamper-evident provenance",
    "C": "Physical CHSH Non-locality",
}
PASS, FAIL, SKIPPED = "pass", "fail", "skipped"


@dataclass(frozen=True)
class LevelResult:
    """Outcome of one verification level: ``status`` is pass, fail, or skipped."""

    level: str
    name: str
    status: str
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == PASS


@dataclass(frozen=True)
class VerificationResult:
    """Overall outcome plus one ``LevelResult`` per level.

    ``is_valid`` is true when every level that ran passed; skipped levels do
    not count. ``evidence_status`` echoes the payload's ``status``, so a
    ``"partial"`` record verifies but is visibly incomplete. Level C runs only
    when the evidence has ``chsh_data``; ``chsh_s_value`` is the CHSH value S
    it computed, or ``None`` when it was skipped or the counts were unusable.
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    levels: dict[str, LevelResult] = field(default_factory=dict)
    evidence_status: str | None = None
    level_c_passed: bool = False
    chsh_s_value: float | None = None


def _level(level: str, errors: list[str]) -> LevelResult:
    return LevelResult(level, LEVEL_NAMES[level], FAIL if errors else PASS, errors)


def _not_run() -> LevelResult:
    return LevelResult("C", LEVEL_NAMES["C"], SKIPPED)


def _level_c(evidence: object) -> tuple[LevelResult, float | None]:
    if not isinstance(evidence, dict) or "chsh_data" not in evidence:
        return _not_run(), None
    _, errors, s_value = verify_level_c(evidence)
    return _level("C", errors), s_value


def verify(evidence: dict | str) -> VerificationResult:
    """Verify an evidence dictionary or its JSON string at every implemented level."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError as exc:
            message = f"Evidence is not valid JSON: {exc}"
            levels = {"A": _level("A", [message]), "B": _level("B", [message]), "C": _not_run()}
            return VerificationResult(False, [message], levels)

    level_c, s_value = _level_c(evidence)
    levels = {
        "A": _level("A", verify_level_a(evidence)[1]),
        "B": _level("B", verify_level_b(evidence)[1]),
        "C": level_c,
    }
    errors = [f"Level {r.level}: {error}" for r in levels.values() for error in r.errors]
    status = evidence.get("status") if isinstance(evidence, dict) else None
    return VerificationResult(
        is_valid=all(r.status != FAIL for r in levels.values()),
        errors=errors,
        levels=levels,
        evidence_status=status if isinstance(status, str) else None,
        level_c_passed=level_c.passed,
        chsh_s_value=s_value,
    )


__all__ = [
    "LevelResult", "VerificationResult", "verify", "verify_level_a", "verify_level_b", "verify_level_c",
]
