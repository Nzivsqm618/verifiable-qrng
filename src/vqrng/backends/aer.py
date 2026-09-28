"""Local Qiskit Aer simulator backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vqrng.backends.base import BackendRun, BaseBackend

if TYPE_CHECKING:
    from qiskit import QuantumCircuit


class AerBackend(BaseBackend):
    def run(self, circuit: QuantumCircuit, shots: int, budget: float | None) -> BackendRun:
        from qiskit_aer import AerSimulator

        simulator = AerSimulator()
        result = simulator.run(circuit, shots=shots, memory=True).result()
        return BackendRun(result.get_memory(), simulator.name)
