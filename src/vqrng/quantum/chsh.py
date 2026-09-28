"""CHSH Bell-test circuits on the |Phi+> state.

Alice (q0) measures at 0 or pi/2 and Bob (q1) at pi/4 or -pi/4 in the X-Z
plane, so each correlation is cos(a - b) and
S = |E(A0,B0) + E(A0,B1) + E(A1,B0) - E(A1,B1)| reaches 2*sqrt(2).
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from qiskit import QuantumCircuit

from vqrng.evidence import CHSH_OUTCOMES, CHSH_SETTINGS

_BOB_ROTATIONS = {"B0": -math.pi / 4, "B1": math.pi / 4}


def generate_chsh_circuits() -> dict[str, QuantumCircuit]:
    """Return one measured Bell-state circuit per setting in ``CHSH_SETTINGS``."""
    circuits: dict[str, QuantumCircuit] = {}
    for setting in CHSH_SETTINGS:
        alice, bob = setting[:2], setting[2:]
        qc = QuantumCircuit(2, 2, name=f"chsh_{setting}")
        qc.h(0)
        qc.cx(0, 1)
        if alice == "A1":
            qc.h(0)
        qc.ry(_BOB_ROTATIONS[bob], 1)
        qc.measure([0, 1], [0, 1])
        circuits[setting] = qc
    return circuits


def tally_counts(bitstrings: Iterable[str]) -> dict[str, int]:
    """Count Qiskit two-bit shots (``c1c0``, Bob's bit first) as Alice-first outcomes."""
    counts = dict.fromkeys(CHSH_OUTCOMES, 0)
    for bits in bitstrings:
        counts[bits[::-1]] += 1
    return counts
