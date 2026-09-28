"""Level C verification: the recorded CHSH counts violate the classical bound.

Computes each correlation E = (N00 + N11 - N01 - N10) / N from
``chsh_data.counts`` and S = |E(A0,B0) + E(A0,B1) + E(A1,B0) - E(A1,B1)|.
Any local hidden-variable model gives S <= 2; |Phi+> reaches 2*sqrt(2).
Standard library only, like Levels A and B.

This checks the counts the backend reported. The pool itself is sampled by a
separate circuit, and Level B only shows the counts were not edited later.
"""

from __future__ import annotations

from typing import Any

from vqrng.evidence import CHSH_CLASSICAL_BOUND, CHSH_OUTCOMES, CHSH_SETTINGS
from vqrng.verifier.level_a import _is_int


def _correlation(setting: str, tally: Any, shots: int) -> tuple[float | None, str | None]:
    label = f"chsh_data.counts.{setting}"
    if not isinstance(tally, dict) or set(tally) != set(CHSH_OUTCOMES):
        return None, f"{label} must map exactly {', '.join(CHSH_OUTCOMES)} to counts."
    if not all(_is_int(n) and n >= 0 for n in tally.values()):
        return None, f"{label} counts must be non-negative integers."
    total = sum(tally.values())
    if total != shots:
        return None, f"{label} holds {total} shots, expected {shots}."
    return (tally["00"] + tally["11"] - tally["01"] - tally["10"]) / total, None


def verify_level_c(evidence: dict) -> tuple[bool, list[str], float | None]:
    """Compute the CHSH value S from ``evidence["chsh_data"]``.

    Returns ``(True, [], S)`` when S > 2, ``(False, [message], S)`` when it
    does not, and ``(False, errors, None)`` when S cannot be computed.
    """
    if not isinstance(evidence, dict):
        return False, [f"Evidence must be an object, got {type(evidence).__name__}."], None
    data = evidence.get("chsh_data")
    if not isinstance(data, dict):
        return False, ["chsh_data is missing."], None
    if data.get("error") is not None:
        return False, [f"the CHSH run did not finish: {data['error']}"], None
    shots = data.get("shots")
    if not _is_int(shots) or shots < 1:
        return False, [f"chsh_data.shots must be a positive integer, got {shots!r}."], None
    counts = data.get("counts")
    counts = counts if isinstance(counts, dict) else {}

    errors: list[str] = []
    correlations: dict[str, float] = {}
    for setting in CHSH_SETTINGS:
        value, error = _correlation(setting, counts.get(setting), shots)
        if error is not None:
            errors.append(error)
        else:
            correlations[setting] = value  # type: ignore[assignment]
    if errors:
        return False, errors, None

    s = abs(correlations["A0B0"] + correlations["A0B1"] + correlations["A1B0"] - correlations["A1B1"])
    if s > CHSH_CLASSICAL_BOUND:
        return True, [], s
    return False, [f"Violation failed: S = {s:.4f} <= {CHSH_CLASSICAL_BOUND}"], s
