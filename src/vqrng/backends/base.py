"""Backend interface shared by the simulator and IBM Quantum hardware."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qiskit import QuantumCircuit


@dataclass(frozen=True)
class BackendRun:
    """Measured bitstrings from one batch, plus where and how long it ran."""

    bitstrings: list[str]
    backend_name: str
    quantum_seconds: float = 0.0
    queue_seconds: float = 0.0
    job_id: str | None = None


class BaseBackend(ABC):
    """Runs the Hadamard measurement circuit and returns one bitstring per shot."""

    @abstractmethod
    def run(self, circuit: QuantumCircuit, shots: int, budget: float | None) -> BackendRun:
        """Execute ``circuit`` for ``shots`` shots within ``budget`` QPU seconds."""

    def __call__(self, circuit: QuantumCircuit, shots: int, budget: float | None) -> BackendRun:
        return self.run(circuit, shots, budget)
