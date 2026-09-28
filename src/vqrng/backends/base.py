"""Backend interface shared by the simulator and IBM Quantum hardware."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from vqrng.evidence import utc_now

if TYPE_CHECKING:
    from qiskit import QuantumCircuit


@dataclass(frozen=True)
class BackendRun:
    """Measured bitstrings from one batch, plus where and how long it ran.

    ``charged_seconds`` is what the batch counts against the QPU budget. ``None``
    means the reported ``quantum_seconds`` are final and are charged as-is.
    ``started_at`` and ``finished_at`` are ISO 8601 UTC times of execution;
    ``None`` lets the caller use the wall-clock time around the call.
    """

    bitstrings: list[str]
    backend_name: str
    quantum_seconds: float = 0.0
    queue_seconds: float = 0.0
    job_id: str | None = None
    charged_seconds: float | None = None
    started_at: str | None = None
    finished_at: str | None = None


class BackendJobError(RuntimeError):
    """A submitted job produced no usable bitstrings.

    Carries the job id and the seconds to charge against the budget so the
    evidence can still account for the job.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        backend_name: str | None = None,
        charged_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.backend_name = backend_name
        self.charged_seconds = charged_seconds


def normalize_bitstrings(bitstrings: Iterable[Any], width: int) -> list[str]:
    """Return every shot as a ``width``-character binary string.

    Restores leading zeros that a result format dropped, and rejects anything
    that is not binary or is wider than the measured register.
    """
    normalized: list[str] = []
    for index, bits in enumerate(bitstrings):
        if not isinstance(bits, str) or not bits or set(bits) - {"0", "1"} or len(bits) > width:
            raise ValueError(f"shot {index} is {bits!r}, not a binary string of at most {width} bits.")
        normalized.append(bits.zfill(width))
    return normalized


def timed_run(
    source: Callable[..., Any], circuit: QuantumCircuit, shots: int, budget: float | None
) -> BackendRun:
    """Call ``source`` once and fill in any execution times it did not report."""
    started = utc_now()
    run = source(circuit, shots, budget)
    finished = utc_now()
    if not isinstance(run, BackendRun):
        run = BackendRun(*run)
    return replace(run, started_at=run.started_at or started, finished_at=run.finished_at or finished)


class BaseBackend(ABC):
    """Runs measurement circuits and returns one bitstring per shot."""

    @abstractmethod
    def run(self, circuit: QuantumCircuit, shots: int, budget: float | None) -> BackendRun:
        """Execute ``circuit`` for ``shots`` shots within ``budget`` QPU seconds."""

    def run_many(
        self, circuits: Sequence[tuple[QuantumCircuit, int]], budget: float | None
    ) -> list[BackendRun]:
        """Execute ``(circuit, shots)`` pairs, one result each, in order.

        Runs them one at a time by default. A backend that can put them in one
        job should, so they share a queue slot and an execution window.
        """
        return [timed_run(self.run, circuit, shots, budget) for circuit, shots in circuits]

    def __call__(self, circuit: QuantumCircuit, shots: int, budget: float | None) -> BackendRun:
        return self.run(circuit, shots, budget)
