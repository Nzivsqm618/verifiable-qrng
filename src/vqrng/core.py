"""Quantum random integer generation with rejection sampling and evidence records."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from qiskit import QuantumCircuit, qasm2

from vqrng.backends import (
    AerBackend,
    BackendJobError,
    BackendRun,
    BaseBackend,
    IBMBackend,
    normalize_bitstrings,
)
from vqrng.evidence import (
    EVIDENCE_VERSION,
    STATUS_COMPLETED,
    STATUS_PARTIAL,
    canonical_json,
    payload_hash,
    resolve_range,
    sha256_hex,
)
from vqrng.quantum.chsh import generate_chsh_circuits, tally_counts

MODES = ("aer", "hardware")
MAX_BATCHES = 64
CHSH_SHOTS = 1024

# (circuit, shots, remaining_budget_seconds) -> BackendRun, or (bitstrings, backend_name, quantum_seconds)
BitSource = Callable[
    [QuantumCircuit, int, "float | None"], "BackendRun | tuple[list[str], str, float]"
]

__all__ = [
    "GenerationError",
    "bits_for_range",
    "build_circuit",
    "canonical_json",
    "format_number",
    "generate",
    "resolve_range",
    "sha256_hex",
]


class GenerationError(RuntimeError):
    """Sampling stopped before the pool filled.

    ``evidence`` is the partial payload (``status == "partial"``) with every
    accepted value, submitted job id, and measured shot up to the failure.
    """

    def __init__(self, message: str, evidence: dict) -> None:
        super().__init__(message)
        self.evidence = evidence


def bits_for_range(range_size: int) -> int:
    return max(1, (range_size - 1).bit_length())


def build_circuit(n_bits: int) -> QuantumCircuit:
    qc = QuantumCircuit(n_bits, n_bits, name="vqrng")
    qc.h(range(n_bits))
    qc.measure(range(n_bits), range(n_bits))
    return qc


def format_number(number: int, digits: int | None, pad: bool) -> str:
    return str(number).zfill(digits) if pad and digits is not None else str(number)


@dataclass
class _Sampling:
    items: list[dict[str, Any]] = field(default_factory=list)
    tape: list[dict[str, Any]] = field(default_factory=list)
    backend_name: str | None = None
    quantum_seconds: float = 0.0
    charged_seconds: float = 0.0
    queue_seconds: float = 0.0
    error: Exception | None = None

    @property
    def job_ids(self) -> list[str]:
        return [batch["job_id"] for batch in self.tape if batch["job_id"] is not None]

    def charge(self, run: BackendRun) -> None:
        self.backend_name = run.backend_name
        self.quantum_seconds += run.quantum_seconds
        self.charged_seconds += run.quantum_seconds if run.charged_seconds is None else run.charged_seconds
        self.queue_seconds += run.queue_seconds

    def remaining(self, runtime_limit: int | None, what: str) -> float | None:
        if runtime_limit is None:
            return None
        remaining = runtime_limit - self.charged_seconds
        if remaining < 1:
            raise RuntimeError(f"QPU runtime budget of {runtime_limit}s exhausted {what}.")
        return remaining

    def record(
        self, job_id: str | None, backend_name: str | None, shots: int,
        bitstrings: list[str], error: str | None = None,
    ) -> None:
        self.tape.append({
            "job_id": job_id,
            "backend": backend_name,
            "shots": shots,
            "bitstrings": bitstrings,
            "error": error,
        })


def _run_batch(
    state: _Sampling, source: BitSource, circuit: QuantumCircuit,
    shots: int, remaining: float | None, n_bits: int,
) -> list[str]:
    try:
        run = source(circuit, shots, remaining)
    except BackendJobError as exc:
        state.backend_name = exc.backend_name or state.backend_name
        state.charged_seconds += exc.charged_seconds
        state.record(exc.job_id, exc.backend_name, shots, [], str(exc))
        raise
    except Exception as exc:
        state.record(None, state.backend_name, shots, [], str(exc))
        raise
    if not isinstance(run, BackendRun):
        run = BackendRun(*run)
    state.charge(run)
    try:
        bitstrings = normalize_bitstrings(run.bitstrings, n_bits)
    except ValueError as exc:
        state.record(run.job_id, run.backend_name, shots, [], str(exc))
        raise
    state.record(run.job_id, run.backend_name, shots, bitstrings)
    return bitstrings


def _fill(
    state: _Sampling, low: int, range_size: int, n_bits: int, pool_size: int,
    circuit: QuantumCircuit, source: BitSource, runtime_limit: int | None,
) -> None:
    rejected: list[str] = []
    acceptance = range_size / 2**n_bits

    for _ in range(MAX_BATCHES):
        remaining = state.remaining(runtime_limit, f"after {len(state.items)}/{pool_size} numbers")
        needed = pool_size - len(state.items)
        shots = max(8, math.ceil(needed / acceptance * 1.25))
        for bits in _run_batch(state, source, circuit, shots, remaining, n_bits):
            if len(state.items) == pool_size:
                break
            candidate = int(bits, 2)
            if candidate >= range_size:
                rejected.append(bits)
                continue
            state.items.append(
                {
                    "index": len(state.items),
                    "number": low + candidate,
                    "bitstring": bits,
                    "rejected": rejected,
                }
            )
            rejected = []
        if len(state.items) == pool_size:
            return
    raise RuntimeError(f"Rejection sampling did not converge after {MAX_BATCHES} batches.")


def _sample_pool(
    low: int, range_size: int, n_bits: int, pool_size: int,
    circuit: QuantumCircuit, source: BitSource, runtime_limit: int | None,
) -> _Sampling:
    """Sample until the pool fills. A failure is kept on the result, not raised,
    so the caller can still build evidence from what was measured."""
    state = _Sampling()
    try:
        _fill(state, low, range_size, n_bits, pool_size, circuit, source, runtime_limit)
    except Exception as exc:
        state.error = exc
    return state


def _run_chsh(state: _Sampling, source: BitSource, shots: int, runtime_limit: int | None) -> dict:
    """Run every CHSH setting once. A failure is recorded in ``error``, not raised."""
    data: dict[str, Any] = {"shots": shots, "counts": {}, "job_ids": [], "error": None}
    try:
        for setting, circuit in generate_chsh_circuits().items():
            remaining = state.remaining(runtime_limit, f"before CHSH setting {setting}")
            try:
                run = source(circuit, shots, remaining)
            except BackendJobError as exc:
                state.charged_seconds += exc.charged_seconds
                if exc.job_id is not None:
                    data["job_ids"].append(exc.job_id)
                raise
            if not isinstance(run, BackendRun):
                run = BackendRun(*run)
            state.charge(run)
            if run.job_id is not None:
                data["job_ids"].append(run.job_id)
            data["counts"][setting] = tally_counts(normalize_bitstrings(run.bitstrings, 2))
    except Exception as exc:
        data["error"] = str(exc)
    return data


def _default_backend(mode: str, name: str | None) -> BaseBackend:
    return IBMBackend(name) if mode == "hardware" else AerBackend()


def generate(
    min_val: int | None = None,
    max_val: int | None = None,
    digits: int | None = None,
    pad: bool = False,
    mode: str = "aer",
    pool_size: int = 1,
    runtime_limit: int | None = None,
    backend: str | BaseBackend | BitSource | None = None,
    *,
    chsh: bool = False,
    chsh_shots: int = CHSH_SHOTS,
    _source: BitSource | None = None,
) -> dict:
    """Generate random integers in an inclusive range.

    Supply either ``min_val`` and ``max_val``, or ``digits`` (with optional
    zero-padding via ``pad``). Returns an evidence dictionary whose ``items``
    carry each ``number``, its zero-padded or plain ``formatted`` string, the
    accepted ``bitstring``, and any ``rejected`` bitstrings preceding it. The
    ``tape`` holds every measured shot per job, including the unused tail of
    the last batch, and ``pool_hash`` commits to the whole payload.

    ``mode="hardware"`` runs on IBM Quantum and requires ``runtime_limit`` (QPU
    seconds) plus an ``IBMQ_API_TOKEN`` or ``QISKIT_IBM_TOKEN`` environment
    variable. ``runtime_limit`` only stops further jobs after the reported QPU
    time is used. It is not sent to IBM, and one job can be billed for more.
    ``backend`` is either the name of a specific QPU (hardware mode;
    by default the least busy one is used) or a ``BaseBackend`` instance, or any
    callable with the same signature, to run on instead of the default.

    ``chsh=True`` also runs the four CHSH Bell-test circuits for ``chsh_shots``
    shots each on the same backend, after the pool fills, and records the
    outcome counts under ``chsh_data`` for Level C. Their QPU time counts
    against ``runtime_limit``.

    Raises ``GenerationError`` if sampling stops before the pool fills, or if
    the CHSH test does not finish; its ``evidence`` attribute holds the payload.
    """
    low, high = resolve_range(min_val, max_val, digits, pad)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}.")
    if pool_size < 1:
        raise ValueError("pool_size must be >= 1.")
    if chsh_shots < 1:
        raise ValueError("chsh_shots must be >= 1.")
    if runtime_limit is not None and runtime_limit < 2:
        raise ValueError("runtime_limit must be >= 2 seconds.")
    if mode == "hardware" and runtime_limit is None:
        raise ValueError("runtime_limit is required in hardware mode.")
    named = backend is None or isinstance(backend, str)
    if not named and not callable(backend):
        raise TypeError(f"backend must be a QPU name or a BaseBackend, got {type(backend).__name__}.")
    if backend is not None and named and mode != "hardware":
        raise ValueError("backend can only be chosen by name in hardware mode.")

    range_size = high - low + 1
    n_bits = bits_for_range(range_size)
    circuit = build_circuit(n_bits)
    circuit_qasm = qasm2.dumps(circuit)
    if _source is not None:
        source: BitSource = _source
    elif named:
        source = _default_backend(mode, backend)  # type: ignore[arg-type]
    else:
        source = backend  # type: ignore[assignment]

    enforced = runtime_limit if mode == "hardware" else None
    started = time.monotonic()
    state = _sample_pool(low, range_size, n_bits, pool_size, circuit, source, enforced)
    chsh_data = _run_chsh(state, source, chsh_shots, enforced) if chsh and state.error is None else None
    wall_seconds = time.monotonic() - started
    for item in state.items:
        item["formatted"] = format_number(item["number"], digits, pad)
    budget_exceeded = enforced is not None and state.charged_seconds > enforced
    if budget_exceeded:
        sys.stderr.write(
            f"vqrng: warning: budget_exceeded: QPU time {state.charged_seconds:.2f}s "
            f"exceeds the {enforced}s budget. IBM billed the time this job used.\n"
        )
        sys.stderr.flush()

    evidence = {
        "version": EVIDENCE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "backend": state.backend_name,
        "status": STATUS_COMPLETED if state.error is None else STATUS_PARTIAL,
        "budget_exceeded": budget_exceeded,
        "error": None if state.error is None else str(state.error),
        "request": {
            "min_val": min_val,
            "max_val": max_val,
            "digits": digits,
            "pad": pad,
            "pool_size": pool_size,
            "runtime_limit": runtime_limit,
            "backend": backend if named else type(backend).__name__,
        },
        "range": {"min": low, "max": high, "size": range_size},
        "n_bits": n_bits,
        "circuit": {"qasm": circuit_qasm, "sha256": sha256_hex(circuit_qasm)},
        "quantum_seconds": state.quantum_seconds,
        "charged_seconds": state.charged_seconds,
        "queue_seconds": state.queue_seconds,
        "wall_seconds": wall_seconds,
        "job_ids": state.job_ids,
        "items": state.items,
        "tape": state.tape,
    }
    if chsh_data is not None:
        evidence["chsh_data"] = chsh_data
    evidence["pool_hash"] = payload_hash(evidence)
    if state.error is not None:
        raise GenerationError(str(state.error), evidence) from state.error
    if chsh_data is not None and chsh_data["error"] is not None:
        raise GenerationError(f"CHSH test did not finish: {chsh_data['error']}", evidence)
    return evidence
