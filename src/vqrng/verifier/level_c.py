"""Level C: a near-real-time CHSH spot-check alongside the pool.

Computes each correlation E = (N00 + N11 - N01 - N10) / N from
``chsh_data.counts`` and S = |E(A0,B0) + E(A0,B1) + E(A1,B0) - E(A1,B1)|.
Any local hidden-variable model gives S <= 2; |Phi+> reaches 2*sqrt(2).
Each CHSH run must also have executed on the pool's backend, within 10
seconds of a pool batch. Standard library only, like Levels A and B.

This is a consistency check on the counts and times the backend reported,
not device-independent certification: the pool is sampled by a separate
circuit, and only a trusted signature (Level B) vouches for who recorded them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vqrng.evidence import (
    CHSH_CLASSICAL_BOUND,
    CHSH_MAX_GAP_SECONDS,
    CHSH_OUTCOMES,
    CHSH_SETTINGS,
    parse_timestamp,
)
from vqrng.verifier.level_a import _is_int

Window = tuple[datetime, datetime]


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


def _window(record: Any) -> Window | None:
    if not isinstance(record, dict):
        return None
    start, end = parse_timestamp(record.get("started_at")), parse_timestamp(record.get("finished_at"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _gap(a: Window, b: Window) -> float:
    """Seconds between two execution windows, or 0 when they overlap."""
    return max(0.0, (b[0] - a[1]).total_seconds(), (a[0] - b[1]).total_seconds())


def _check_binding(evidence: dict, data: dict) -> list[str]:
    tape = evidence.get("tape")
    batches = tape if isinstance(tape, list) else []
    pool = [
        window for batch in batches
        if isinstance(batch, dict) and batch.get("error") is None and (window := _window(batch)) is not None
    ]
    if not pool:
        return ["no pool batch records valid execution times, so the CHSH runs cannot be tied to the pool."]
    runs = data.get("runs")
    runs = runs if isinstance(runs, dict) else {}
    backend = evidence.get("backend")

    errors: list[str] = []
    for setting in CHSH_SETTINGS:
        run = runs.get(setting)
        window = _window(run)
        if window is None:
            errors.append(
                f"chsh_data.runs.{setting} has no valid started_at/finished_at; "
                "the CHSH run is decoupled from the pool."
            )
            continue
        if run.get("backend") != backend:
            errors.append(f"CHSH setting {setting} ran on {run.get('backend')!r}, but the pool ran on {backend!r}.")
        gap = min(_gap(window, batch) for batch in pool)
        if gap > CHSH_MAX_GAP_SECONDS:
            errors.append(
                f"CHSH setting {setting} ran {gap:.1f}s from the nearest pool batch, "
                f"outside the {CHSH_MAX_GAP_SECONDS:g}s window."
            )
    return errors


def verify_level_c(evidence: dict) -> tuple[bool, list[str], float | None]:
    """Compute the CHSH value S from ``evidence["chsh_data"]`` and check its timing.

    Returns ``(True, [], S)`` when S > 2 and every CHSH run is bound to the
    pool, ``(False, errors, S)`` when either fails, and ``(False, errors, None)``
    when S cannot be computed.
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
    errors = _check_binding(evidence, data)
    if s <= CHSH_CLASSICAL_BOUND:
        errors.append(f"Violation failed: S = {s:.4f} <= {CHSH_CLASSICAL_BOUND}")
    return not errors, errors, s
