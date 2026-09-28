"""QSeed: expand one recorded 32-byte seed into a fast local PCG64 stream.

For Monte Carlo, simulation, and test data where a QPU call per number is too
slow. The evidence covers the seed job only. Every expanded number is
classical and deterministic from the seed, and the seed is in the evidence,
so anyone holding the record can replay the stream. This is not a CSPRNG and
not quantum output: do not use it for keys, tokens, or anything an adversary
must not predict. The same stream also needs the same NumPy version, since
NumPy does not promise ``Generator.integers`` stays identical across releases.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from vqrng.evidence import KIND_SEED, QSEED_EXPANDER, SEED_BYTES, STATUS_COMPLETED, payload_hash


class QSeedError(ValueError):
    """The evidence cannot seed a QSeed stream."""


def _load(evidence: dict | str) -> dict:
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError as exc:
            raise QSeedError(f"the seed record is not valid JSON: {exc}") from exc
    if not isinstance(evidence, dict):
        raise QSeedError(f"the seed record must be an object, got {type(evidence).__name__}.")
    return evidence


def _seed_bytes(evidence: dict, allow_simulator: bool) -> bytes:
    kind = evidence.get("kind")
    if kind != KIND_SEED:
        raise QSeedError(f"the evidence is not a seed record (kind is {kind!r}); create one with 'vqrng seed'.")
    if evidence.get("pool_hash") != payload_hash(evidence):
        raise QSeedError("pool_hash does not match the seed record; it was changed or truncated.")
    if evidence.get("expander") != QSEED_EXPANDER:
        raise QSeedError(f"expander is {evidence.get('expander')!r}, expected {QSEED_EXPANDER!r}.")
    seed = evidence.get("seed")
    try:
        raw = bytes.fromhex(seed) if evidence.get("status") == STATUS_COMPLETED else b""
    except (TypeError, ValueError):
        raw = b""
    if len(raw) != SEED_BYTES:
        raise QSeedError(f"the seed record holds no complete {SEED_BYTES}-byte seed (status is "
                         f"{evidence.get('status')!r}, seed is {seed!r}).")
    mode = evidence.get("mode")
    if mode != "hardware" and not allow_simulator:
        raise QSeedError(
            f"the seed came from mode {mode!r}, not IBM hardware; the simulator is itself a PRNG. "
            "Pass allow_simulator=True (--allow-simulator) to expand it anyway."
        )
    return raw


class QSeed:
    """A NumPy ``PCG64`` generator seeded from a ``vqrng.collect_seed`` record.

    ``QSeed`` checks that the record is a completed seed record whose
    ``pool_hash`` still matches, and that it came from IBM hardware unless
    ``allow_simulator`` is set. It does not run ``vqrng.verify``; do that on
    the record to check its tape, health tests, and signature.
    """

    def __init__(self, evidence: dict | str, *, allow_simulator: bool = False) -> None:
        record = _load(evidence)
        self.seed = _seed_bytes(record, allow_simulator)
        self.pool_hash: str = record["pool_hash"]
        self.generator = np.random.Generator(np.random.PCG64(int.from_bytes(self.seed, "big")))

    def integers(self, low: int, high: int, size: Any = None) -> Any:
        """Integers in ``[low, high]``, inclusive like the rest of vqrng."""
        if low > high:
            raise ValueError(f"low ({low}) must be <= high ({high}).")
        return self.generator.integers(low, high, size=size, endpoint=True)
