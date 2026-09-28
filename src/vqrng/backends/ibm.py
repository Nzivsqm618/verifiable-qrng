"""IBM Quantum hardware backend using Qiskit Runtime SamplerV2."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vqrng.backends.base import BackendJobError, BackendRun, BaseBackend, normalize_bitstrings
from vqrng.evidence import parse_timestamp, sha256_hex

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
    return parse_timestamp(value)


def execution_window_from_metrics(metrics: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return IBM's ``running`` and ``finished`` times as ISO 8601 UTC, where reported."""
    timestamps = metrics.get("timestamps") or {}
    window = []
    for key in ("running", "finished"):
        parsed = _parse_time(timestamps.get(key))
        window.append(None if parsed is None or parsed.tzinfo is None else parsed.astimezone(timezone.utc).isoformat())
    return window[0], window[1]


def queue_seconds_from_metrics(metrics: Mapping[str, Any]) -> float | None:
    timestamps = metrics.get("timestamps") or {}
    created = _parse_time(timestamps.get("created"))
    running = _parse_time(timestamps.get("running"))
    if created is None or running is None:
        return None
    return max(0.0, (running - created).total_seconds())


def quantum_seconds_from_metrics(metrics: Mapping[str, Any]) -> float | None:
    """Return the reported QPU seconds, or ``None`` when IBM did not report them."""
    usage = metrics.get("usage") or {}
    for key in ("quantum_seconds", "qpu_charge_time_seconds"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _measured_register(data: Any, name: str) -> Any:
    register = getattr(data, name, None)
    if hasattr(register, "get_bitstrings"):
        return register
    try:
        register = data[name]
    except (KeyError, TypeError):
        register = None
    if hasattr(register, "get_bitstrings"):
        return register
    values = getattr(data, "values", None)
    candidates = [r for r in values() if hasattr(r, "get_bitstrings")] if callable(values) else []
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"result has no classical register named {name!r}")


def read_bitstrings(pub_result: Any, register_name: str, width: int) -> list[str]:
    """Read one classical register of a SamplerV2 pub result as ``width``-bit strings.

    Looks the register up by name, falling back to the only register present,
    since transpilation and result formats do not always keep the name.
    """
    register = _measured_register(pub_result.data, register_name)
    return normalize_bitstrings(register.get_bitstrings(), width)


def extract_bitstrings(pub_result: Any, circuit: QuantumCircuit) -> list[str]:
    """Read the measured register of a SamplerV2 pub result as fixed-width bitstrings."""
    return read_bitstrings(pub_result, circuit.cregs[0].name, circuit.num_clbits)


def circuit_sha256(circuit: Any) -> str | None:
    """SHA-256 of a circuit's OpenQASM 3 text, or ``None`` if it cannot be exported."""
    from qiskit import qasm3

    try:
        text = qasm3.dumps(circuit)
    except Exception:
        return None
    return sha256_hex(text)


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
        return self.run_many([(circuit, shots)], budget)[0]

    def run_many(
        self, circuits: Sequence[tuple[QuantumCircuit, int]], budget: float | None
    ) -> list[BackendRun]:
        """Run every ``(circuit, shots)`` pair as one pub of a single Sampler job.

        The job's QPU and queue time are reported on the first result only, so
        adding the results up does not count them twice.
        """
        from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
        from qiskit_ibm_runtime import SamplerV2

        backend = self._select_backend(max(circuit.num_qubits for circuit, _ in circuits))
        pass_manager = generate_preset_pass_manager(optimization_level=1, backend=backend)
        isa_circuits = [pass_manager.run(circuit) for circuit, _ in circuits]
        isa_hashes = [circuit_sha256(isa) for isa in isa_circuits]
        pubs = [(isa, None, shots) for isa, (_, shots) in zip(isa_circuits, circuits)]
        sampler = SamplerV2(mode=backend)
        # Leave max_execution_time unset. IBM cancels the job when that limit
        # trips and still bills the QPU time already used, so the option spends
        # credit and returns no shots. ``budget`` only decides later batches.

        submitted = self._clock()
        job = sampler.run(pubs)
        job_id = job.job_id()
        shot_list = ", ".join(str(shots) for _, shots in circuits)
        self._log(f"job {job_id} submitted to {backend.name} ({shot_list} shots)")

        def failed(message: str) -> BackendJobError:
            # Usage is unknown, so count the whole remaining budget and stop.
            charged = 0.0 if budget is None else float(budget)
            return BackendJobError(
                message, job_id=job_id, backend_name=backend.name, charged_seconds=charged
            )

        try:
            status, running_at = self._poll(job, job_id, submitted)
        except KeyboardInterrupt:
            self._log(f"job {job_id} interrupted; cancelling so it does not use more QPU time")
            self._cancel(job, job_id)
            raise
        except Exception as exc:
            self._log(f"job {job_id}: lost track of the job ({exc}); cancelling so it does not use more QPU time")
            self._cancel(job, job_id)
            raise failed(f"IBM job {job_id} on {backend.name} could not be polled: {exc}") from exc
        finished = self._clock()

        if status != "DONE":
            detail = job.error_message() if status == "ERROR" else None
            suffix = f": {detail}" if detail else ""
            raise failed(f"IBM job {job_id} on {backend.name} ended with status {status}{suffix}")

        try:
            results = job.result()
            measured = [extract_bitstrings(results[i], circuit) for i, (circuit, _) in enumerate(circuits)]
        except Exception as exc:
            raise failed(f"IBM job {job_id} on {backend.name} returned an unreadable result: {exc}") from exc

        metrics, final = self._final_metrics(job, job_id)
        reported = quantum_seconds_from_metrics(metrics)
        quantum_seconds = 0.0 if reported is None else reported
        charged_seconds: float | None = None
        if (not final or reported is None) and budget is not None:
            charged_seconds = max(quantum_seconds, float(budget))
            self._log(
                f"job {job_id}: QPU usage is unknown; counting the full {budget:g}s "
                "budget so no further job is submitted"
            )
        queue_seconds = queue_seconds_from_metrics(metrics)
        if queue_seconds is None:
            # Without server timestamps, queue time is only known to the poll interval.
            queue_seconds = (finished if running_at is None else running_at) - submitted

        started_at, finished_at = execution_window_from_metrics(metrics)

        self._log(
            f"job {job_id} DONE: {quantum_seconds:.2f}s QPU, {queue_seconds:.1f}s queued, "
            f"{finished - submitted:.1f}s total"
        )
        first = BackendRun(
            measured[0], backend.name, quantum_seconds, queue_seconds, job_id, charged_seconds,
            started_at, finished_at, isa_hashes[0],
        )
        rest = replace(first, quantum_seconds=0.0, queue_seconds=0.0, charged_seconds=None)
        return [first] + [
            replace(rest, bitstrings=bits, isa_sha256=digest) for bits, digest in zip(measured[1:], isa_hashes[1:])
        ]

    def _cancel(self, job: Any, job_id: str) -> None:
        try:
            job.cancel()
        except Exception as exc:
            self._log(f"job {job_id}: cancel failed ({exc}); check it on IBM Quantum")

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

    def _final_metrics(self, job: Any, job_id: str) -> tuple[Mapping[str, Any], bool]:
        """Return the job metrics and whether IBM has finalised the usage in them."""
        metrics: Mapping[str, Any] = {}
        for attempt in range(USAGE_ATTEMPTS):
            try:
                metrics = job.metrics() or {}
            except Exception as exc:
                self._log(f"job {job_id}: could not read job metrics ({exc})")
                return {}, False
            if (metrics.get("usage") or {}).get("status") != "pending":
                return metrics, True
            if attempt < USAGE_ATTEMPTS - 1:
                self._sleep(USAGE_RETRY_SECONDS)
        self._log(f"job {job_id}: IBM has not finalised QPU usage yet; recording the partial value")
        return metrics, False
