"""Level B verification: the evidence is unaltered and internally consistent.

Recomputes the circuit and payload hashes, checks that the request explains
the range and the status explains the item count, and replays the items from
the recorded shot tape. Standard library only, like Level A.

Without a signing key these checks detect edits to a payload, not a payload
forged from scratch with fresh hashes.
"""

from __future__ import annotations

from typing import Any

from vqrng.evidence import (
    EVIDENCE_VERSION,
    STATUS_COMPLETED,
    STATUS_PARTIAL,
    STATUSES,
    payload_hash,
    resolve_range,
    sha256_hex,
)
from vqrng.verifier.level_a import _is_int, _parse_bits


def _check_hashes(evidence: dict) -> list[str]:
    errors: list[str] = []
    circuit = evidence.get("circuit")
    circuit = circuit if isinstance(circuit, dict) else {}
    qasm, recorded = circuit.get("qasm"), circuit.get("sha256")
    if not isinstance(qasm, str):
        errors.append("circuit.qasm is missing.")
    elif not isinstance(recorded, str):
        errors.append("circuit.sha256 is missing.")
    elif recorded != sha256_hex(qasm):
        errors.append("circuit.sha256 does not match the SHA-256 of circuit.qasm.")

    pool_hash = evidence.get("pool_hash")
    if not isinstance(pool_hash, str):
        errors.append("pool_hash is missing.")
    elif pool_hash != payload_hash(evidence):
        errors.append("pool_hash does not match the evidence payload; it was changed after generation.")
    return errors


def _check_request(evidence: dict) -> list[str]:
    request = evidence.get("request")
    if not isinstance(request, dict):
        return ["request is missing."]
    try:
        expected = resolve_range(
            request.get("min_val"), request.get("max_val"), request.get("digits"), bool(request.get("pad"))
        )
    except (TypeError, ValueError) as exc:
        return [f"request does not describe a valid range: {exc}"]
    range_info = evidence.get("range")
    recorded = (range_info.get("min"), range_info.get("max")) if isinstance(range_info, dict) else None
    if recorded != expected:
        return [f"range is {recorded!r}, but the request resolves to {expected!r}."]
    return []


def _check_status(evidence: dict) -> list[str]:
    status = evidence.get("status")
    if status not in STATUSES:
        return [f"status is {status!r}, expected one of {STATUSES}."]
    request = evidence.get("request")
    pool_size = request.get("pool_size") if isinstance(request, dict) else None
    if not _is_int(pool_size) or pool_size < 1:
        return [f"request.pool_size must be a positive integer, got {pool_size!r}."]
    items = evidence.get("items")
    count = len(items) if isinstance(items, list) else 0
    if status == STATUS_COMPLETED and count != pool_size:
        return [f"status is {status!r} but the evidence holds {count} of {pool_size} values."]
    if status == STATUS_PARTIAL and count >= pool_size:
        return [f"status is {status!r} but the evidence holds all {pool_size} values."]
    return []


def _replay(evidence: dict, shots: list[Any]) -> list[str]:
    range_info = evidence.get("range")
    range_info = range_info if isinstance(range_info, dict) else {}
    low, high = range_info.get("min"), range_info.get("max")
    n_bits, items = evidence.get("n_bits"), evidence.get("items")
    if not (_is_int(low) and _is_int(high) and _is_int(n_bits) and isinstance(items, list)):
        return []  # Level A reports the malformed range, bit width, or items.

    consumed: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            rejected = item.get("rejected", [])
            consumed.extend(rejected if isinstance(rejected, list) else [])
            consumed.append(item.get("bitstring"))
    if shots[: len(consumed)] != consumed:
        pairs = enumerate(zip(consumed, shots))
        position = next((i for i, (want, got) in pairs if want != got), min(len(consumed), len(shots)))
        return [f"items do not replay from the shot tape; first difference at shot {position}."]

    errors: list[str] = []
    range_size = high - low + 1
    for offset, bits in enumerate(shots[len(consumed):], start=len(consumed)):
        candidate = _parse_bits(bits, n_bits)
        if candidate is None:
            errors.append(f"tape shot {offset} {bits!r} is not a {n_bits}-bit binary string.")
        elif evidence.get("status") == STATUS_PARTIAL and candidate < range_size:
            errors.append(
                f"tape shot {offset} decodes to {candidate}, within range size {range_size}; "
                "a partial run would have accepted it."
            )
    return errors


def _check_tape(evidence: dict) -> list[str]:
    tape = evidence.get("tape")
    if not isinstance(tape, list):
        return ["tape must be a list of job batches."]
    errors: list[str] = []
    shots: list[Any] = []
    job_ids: list[Any] = []
    for position, batch in enumerate(tape):
        bits = batch.get("bitstrings") if isinstance(batch, dict) else None
        if not isinstance(bits, list):
            errors.append(f"tape[{position}]: expected an object with a 'bitstrings' list.")
            continue
        shots.extend(bits)
        if batch.get("job_id") is not None:
            job_ids.append(batch["job_id"])
    if evidence.get("job_ids") != job_ids:
        errors.append(f"job_ids {evidence.get('job_ids')!r} do not match the tape's jobs {job_ids!r}.")
    errors.extend(_replay(evidence, shots))
    return errors


def verify_level_b(evidence: dict) -> tuple[bool, list[str]]:
    """Check the hashes, request, status, and shot tape of ``evidence``.

    Returns ``(True, [])`` when all of them hold, otherwise ``(False, errors)``.
    """
    if not isinstance(evidence, dict):
        return False, [f"Evidence must be an object, got {type(evidence).__name__}."]

    errors: list[str] = []
    version = evidence.get("version")
    if version != EVIDENCE_VERSION:
        errors.append(f"version is {version!r}, expected {EVIDENCE_VERSION!r}.")
    errors.extend(_check_hashes(evidence))
    errors.extend(_check_request(evidence))
    errors.extend(_check_status(evidence))
    errors.extend(_check_tape(evidence))
    return not errors, errors
