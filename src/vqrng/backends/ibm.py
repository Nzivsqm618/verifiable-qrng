"""IBM Quantum hardware backend using Qiskit Runtime SamplerV2."""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from vqrng.backends.base import BackendRun, BaseBackend

if TYPE_CHECKING:
    from qiskit import QuantumCircuit

TOKEN_ENV_VARS = ("IBMQ_API_TOKEN", "QISKIT_IBM_TOKEN")
INSTANCE_ENV_VAR = "QISKIT_IBM_INSTANCE"
CHANNEL = "ibm_quantum_platform"

FINAL_STATUSES = ("DONE", "ERROR", "CANCELLED")
_STATUS_ALIASES = {"COMPLETED": "DONE", "FAILED": "ERROR", "CANCELED": "CANCELLED"}

POLL_SECONDS = 5.0
HEARTBEAT_SECONDS = 60.0
USAGE_ATTEMPTS = 5
USAGE_RETRY_SECONDS = 2.0


def read_token(environ: Mapping[str, str] | None = None) -> str:
    """Return the IBM Quantum API token, checking ``IBMQ_API_TOKEN`` first."""
    env = os.environ if environ is None else environ
    for name in TOKEN_ENV_VARS:
        value = env.get(name, "").strip()
        if value:
            return value
    raise ValueError(
        "IBM Quantum hardware needs an API token. "
        "Set IBMQ_API_TOKEN or QISKIT_IBM_TOKEN in your environment."
    )


def stderr_log(message: str) -> None:
    sys.stderr.write(f"vqrng: [{datetime.now():%H:%M:%S}] {message}\n")
    sys.stderr.flush()


def status_name(status: Any) -> str:
    """Normalise a Runtime job status (string or enum) to an upper-case name."""
    name = str(getattr(status, "name", status)).upper()
    return _STATUS_ALIASES.get(name, name)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def queue_seconds_from_metrics(metrics: Mapping[str, Any]) -> float | None:
    timestamps = metrics.get("timestamps") or {}
    created = _parse_time(timestamps.get("created"))
    running = _parse_time(timestamps.get("running"))
    if created is None or running is None:
        return None
    return max(0.0, (running - created).total_seconds())


def quantum_seconds_from_metrics(metrics: Mapping[str, Any]) -> float:
    usage = metrics.get("usage") or {}
    for key in ("quantum_seconds", "qpu_charge_time_seconds"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


class IBMBackend(BaseBackend):
    """Run circuits on a physical IBM Quantum QPU.

    Selects ``backend_name`` when given, otherwise the least-busy operational
    hardware backend with enough qubits. Job status is polled and reported via
    ``log`` (stderr by default) so stdout stays clean for numbers and JSON.
    """

    def __init__(
        self,
        backend_name: str | None = None,
        *,
        token: str | None = None,
        instance: str | None = None,
        poll_interval: float | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend_name = backend_name
        self._token = token or read_token()
        self._instance = instance or os.environ.get(INSTANCE_ENV_VAR) or None
        self._poll_interval = POLL_SECONDS if poll_interval is None else poll_interval
        self._log = log or stderr_log
        self._clock = clock
        self._sleep = sleep
        self._backend: Any = None

    def _select_backend(self, num_qubits: int) -> Any:
        if self._backend is None:
            from qiskit_ibm_runtime import QiskitRuntimeService

            service = QiskitRuntimeService(channel=CHANNEL, token=self._token, instance=self._instance)
            if self.backend_name:
                self._backend = service.backend(self.backend_name)
            else:
                self._backend = service.least_busy(
                    operational=True, simulator=False, min_num_qubits=num_qubits
                )
            self._log(f"selected IBM backend {self._backend.name}")
        return self._backend

    def run(self, circuit: QuantumCircuit, shots: int, budget: float | None) -> BackendRun:
        from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
        from qiskit_ibm_runtime import SamplerV2

        backend = self._select_backend(circuit.num_qubits)
        isa_circuit = generate_preset_pass_manager(optimization_level=1, backend=backend).run(circuit)
        sampler = SamplerV2(mode=backend)
        if budget is not None:
            sampler.options.max_execution_time = max(1, math.floor(budget))

        submitted = self._clock()
        job = sampler.run([isa_circuit], shots=shots)
        job_id = job.job_id()
        self._log(f"job {job_id} submitted to {backend.name} ({shots} shots)")

        try:
            status, running_at = self._poll(job, job_id, submitted)
        except KeyboardInterrupt:
            self._log(f"job {job_id} interrupted; cancelling so it does not use more QPU time")
            job.cancel()
            raise
        finished = self._clock()

        if status != "DONE":
            detail = job.error_message() if status == "ERROR" else None
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"IBM job {job_id} on {backend.name} ended with status {status}{suffix}")

        bitstrings = job.result()[0].data[circuit.cregs[0].name].get_bitstrings()
        metrics = self._final_metrics(job, job_id)
        quantum_seconds = quantum_seconds_from_metrics(metrics)
        queue_seconds = queue_seconds_from_metrics(metrics)
        if queue_seconds is None:
            # Without server timestamps, queue time is only known to the poll interval.
            queue_seconds = (finished if running_at is None else running_at) - submitted

        self._log(
            f"job {job_id} DONE: {quantum_seconds:.2f}s QPU, {queue_seconds:.1f}s queued, "
            f"{finished - submitted:.1f}s total"
        )
        return BackendRun(bitstrings, backend.name, quantum_seconds, queue_seconds, job_id)

    def _poll(self, job: Any, job_id: str, submitted: float) -> tuple[str, float | None]:
        last_status: str | None = None
        last_report = submitted
        running_at: float | None = None
        while True:
            status = status_name(job.status())
            now = self._clock()
            if status != last_status:
                self._log(f"job {job_id} {status}")
                last_status, last_report = status, now
            elif now - last_report >= HEARTBEAT_SECONDS:
                self._log(f"job {job_id} still {status} ({now - submitted:.0f}s elapsed)")
                last_report = now
            if status == "RUNNING" and running_at is None:
                running_at = now
            if status in FINAL_STATUSES:
                return status, running_at
            self._sleep(self._poll_interval)

    def _final_metrics(self, job: Any, job_id: str) -> Mapping[str, Any]:
        metrics: Mapping[str, Any] = {}
        for attempt in range(USAGE_ATTEMPTS):
            try:
                metrics = job.metrics() or {}
            except Exception as exc:
                self._log(f"job {job_id}: could not read job metrics ({exc}); QPU time recorded as 0")
                return {}
            if (metrics.get("usage") or {}).get("status") != "pending":
                return metrics
            if attempt < USAGE_ATTEMPTS - 1:
                self._sleep(USAGE_RETRY_SECONDS)
        self._log(f"job {job_id}: IBM has not finalised QPU usage yet; recording the partial value")
        return metrics
