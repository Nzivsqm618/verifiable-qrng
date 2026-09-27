"""Quantum random integer generation with rejection sampling and evidence records."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from qiskit import QuantumCircuit, qasm2

EVIDENCE_VERSION = "1"
MODES = ("aer", "hardware")
MAX_BATCHES = 64

# (circuit, shots, remaining_budget_seconds) -> (bitstrings, backend_name, quantum_seconds_used)
BitSource = Callable[[QuantumCircuit, int, "float | None"], "tuple[list[str], str, float]"]


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


def bits_for_range(range_size: int) -> int:
    return max(1, (range_size - 1).bit_length())


def build_circuit(n_bits: int) -> QuantumCircuit:
    qc = QuantumCircuit(n_bits, n_bits, name="vqrng")
    qc.h(range(n_bits))
    qc.measure(range(n_bits), range(n_bits))
    return qc


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def format_number(number: int, digits: int | None, pad: bool) -> str:
    return str(number).zfill(digits) if pad and digits is not None else str(number)


def aer_source(circuit: QuantumCircuit, shots: int, budget: float | None) -> tuple[list[str], str, float]:
    from qiskit_aer import AerSimulator

    backend = AerSimulator()
    result = backend.run(circuit, shots=shots, memory=True).result()
    return result.get_memory(), backend.name, 0.0


def hardware_source(circuit: QuantumCircuit, shots: int, budget: float | None) -> tuple[list[str], str, float]:
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2

    token = os.environ.get("IBMQ_API_TOKEN")
    service = QiskitRuntimeService(token=token) if token else QiskitRuntimeService()
    backend = service.least_busy(operational=True, simulator=False, min_num_qubits=circuit.num_qubits)

    isa_circuit = generate_preset_pass_manager(optimization_level=1, backend=backend).run(circuit)
    sampler = SamplerV2(mode=backend)
    if budget is not None:
        sampler.options.max_execution_time = max(1, math.floor(budget))

    job = sampler.run([isa_circuit], shots=shots)
    bitstrings = job.result()[0].data[circuit.cregs[0].name].get_bitstrings()
    used = float(job.usage() or 0.0)
    return bitstrings, backend.name, used


def _sample_pool(
    low: int,
    range_size: int,
    n_bits: int,
    pool_size: int,
    circuit: QuantumCircuit,
    source: BitSource,
    runtime_limit: int | None,
) -> tuple[list[dict[str, Any]], str | None, float]:
    items: list[dict[str, Any]] = []
    rejected: list[str] = []
    backend_name: str | None = None
    quantum_seconds = 0.0
    acceptance = range_size / 2**n_bits

    for _ in range(MAX_BATCHES):
        needed = pool_size - len(items)
        if needed == 0:
            break

        remaining = None
        if runtime_limit is not None:
            remaining = runtime_limit - quantum_seconds
            if remaining <= 0:
                raise RuntimeError(
                    f"QPU runtime budget of {runtime_limit}s exhausted after "
                    f"{len(items)}/{pool_size} numbers."
                )

        shots = max(8, math.ceil(needed / acceptance * 1.25))
        bitstrings, backend_name, used = source(circuit, shots, remaining)
        quantum_seconds += used

        for bits in bitstrings:
            candidate = int(bits, 2)
            if candidate >= range_size:
                rejected.append(bits)
                continue
            items.append(
                {
                    "index": len(items),
                    "number": low + candidate,
                    "bitstring": bits,
                    "rejected": rejected,
                }
            )
            rejected = []
            if len(items) == pool_size:
                break
    else:
        raise RuntimeError(f"Rejection sampling did not converge after {MAX_BATCHES} batches.")

    return items, backend_name, quantum_seconds


def generate(
    min_val: int | None = None,
    max_val: int | None = None,
    digits: int | None = None,
    pad: bool = False,
    mode: str = "aer",
    pool_size: int = 1,
    runtime_limit: int | None = None,
    *,
    _source: BitSource | None = None,
) -> dict:
    """Generate random integers in an inclusive range.

    Supply either ``min_val`` and ``max_val``, or ``digits`` (with optional
    zero-padding via ``pad``). Returns an evidence dictionary whose ``items``
    carry each ``number``, its zero-padded or plain ``formatted`` string, the
    accepted ``bitstring``, and any ``rejected`` bitstrings preceding it.
    """
    low, high = resolve_range(min_val, max_val, digits, pad)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}.")
    if pool_size < 1:
        raise ValueError("pool_size must be >= 1.")
    if runtime_limit is not None and runtime_limit < 2:
        raise ValueError("runtime_limit must be >= 2 seconds.")
    if mode == "hardware" and runtime_limit is None:
        raise ValueError("runtime_limit is required in hardware mode.")

    range_size = high - low + 1
    n_bits = bits_for_range(range_size)
    circuit = build_circuit(n_bits)
    circuit_qasm = qasm2.dumps(circuit)
    source = _source or (aer_source if mode == "aer" else hardware_source)

    items, backend_name, quantum_seconds = _sample_pool(
        low, range_size, n_bits, pool_size, circuit, source,
        runtime_limit if mode == "hardware" else None,
    )
    for item in items:
        item["formatted"] = format_number(item["number"], digits, pad)

    return {
        "version": EVIDENCE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "backend": backend_name,
        "request": {
            "min_val": min_val,
            "max_val": max_val,
            "digits": digits,
            "pad": pad,
            "pool_size": pool_size,
            "runtime_limit": runtime_limit,
        },
        "range": {"min": low, "max": high, "size": range_size},
        "n_bits": n_bits,
        "circuit": {"qasm": circuit_qasm, "sha256": sha256_hex(circuit_qasm)},
        "quantum_seconds": quantum_seconds,
        "items": items,
        "pool_hash": sha256_hex(canonical_json(items)),
    }
