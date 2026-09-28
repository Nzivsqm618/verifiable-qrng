"""Verification of vqrng evidence payloads: offline Levels A-C, plus an optional live IBM check."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from vqrng.verifier.ibm_live import check_ibm_jobs
from vqrng.verifier.level_a import verify_level_a
from vqrng.verifier.level_b import (
    ASSURANCE_AUTHENTIC,
    ASSURANCE_CHECKSUM,
    ASSURANCE_SIGNED,
    check_level_b,
    verify_level_b,
)
from vqrng.verifier.level_c import verify_level_c

LEVEL_NAMES = {
    "A": "reproducible conversion",
    "B": "tamper-evident provenance",
    "C": "near-real-time CHSH spot-check",
    "IBM": "live IBM job check",
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
    ``"partial"`` record verifies but is visibly incomplete.

    ``level_b_assurance`` says how Level B passed: ``"checksum-only"`` (the
    payload is self-consistent), ``"signed-untrusted-key"`` (validly signed,
    but by a key nobody vouched for), or ``"authentic"`` (signed by one of the
    trusted keys). It is ``None`` when Level B failed.

    Level C runs only when the evidence has ``chsh_data``, unless it was
    required; ``chsh_s_value`` is the CHSH value S it computed, or ``None``
    when it was skipped or the counts were unusable.

    ``levels["IBM"]`` is present only when the live IBM check was requested;
    ``notes`` lists what that check could not compare.
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    levels: dict[str, LevelResult] = field(default_factory=dict)
    evidence_status: str | None = None
    level_b_assurance: str | None = None
    level_c_passed: bool = False
    chsh_s_value: float | None = None
    notes: list[str] = field(default_factory=list)


def _level(level: str, errors: list[str]) -> LevelResult:
    return LevelResult(level, LEVEL_NAMES[level], FAIL if errors else PASS, errors)


def _not_run() -> LevelResult:
    return LevelResult("C", LEVEL_NAMES["C"], SKIPPED)


def _level_c(evidence: object, require_chsh: bool) -> tuple[LevelResult, float | None]:
    if not isinstance(evidence, dict) or "chsh_data" not in evidence:
        if require_chsh:
            return _level("C", ["chsh_data is missing, but a CHSH spot-check is required."]), None
        return _not_run(), None
    _, errors, s_value = verify_level_c(evidence)
    return _level("C", errors), s_value


def verify(
    evidence: dict | str,
    trusted_keys: Iterable[str] | None = None,
    *,
    require_chsh: bool = False,
    ibm: bool = False,
    ibm_service: Any = None,
) -> VerificationResult:
    """Verify an evidence dictionary or its JSON string at every level.

    ``trusted_keys`` are hex Ed25519 public keys the caller already trusts.
    When given, Level B fails unless the evidence is signed by one of them.
    ``require_chsh`` makes Level C fail, instead of being skipped, when the
    evidence has no CHSH data. ``ibm`` adds the live IBM job check, which
    contacts IBM through ``ibm_service`` or a service opened with the token
    in the environment. Levels A-C never touch the network.
    """
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError as exc:
            message = f"Evidence is not valid JSON: {exc}"
            levels = {"A": _level("A", [message]), "B": _level("B", [message]),
                      "C": _level("C", [message]) if require_chsh else _not_run()}
            if ibm:
                levels["IBM"] = _level("IBM", [message])
            return VerificationResult(False, [message], levels)

    level_b_errors, assurance = check_level_b(evidence, trusted_keys)
    level_c, s_value = _level_c(evidence, require_chsh)
    levels = {
        "A": _level("A", verify_level_a(evidence)[1]),
        "B": _level("B", level_b_errors),
        "C": level_c,
    }
    notes: list[str] = []
    if ibm:
        ibm_errors, notes = check_ibm_jobs(evidence, ibm_service)
        levels["IBM"] = _level("IBM", ibm_errors)
    errors = [f"Level {r.level}: {error}" for r in levels.values() for error in r.errors]
    status = evidence.get("status") if isinstance(evidence, dict) else None
    return VerificationResult(
        is_valid=all(r.status != FAIL for r in levels.values()),
        errors=errors,
        levels=levels,
        evidence_status=status if isinstance(status, str) else None,
        level_b_assurance=assurance,
        level_c_passed=level_c.passed,
        chsh_s_value=s_value,
        notes=notes,
    )


__all__ = [
    "ASSURANCE_AUTHENTIC", "ASSURANCE_CHECKSUM", "ASSURANCE_SIGNED",
    "LevelResult", "VerificationResult", "verify", "verify_level_a", "verify_level_b", "verify_level_c",
]
