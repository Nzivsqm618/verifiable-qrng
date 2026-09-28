"""Live check of evidence against the IBM Quantum jobs it names.

Unlike Levels A-C, this contacts IBM, with the caller's own token. For each
job on the tape it asks IBM for the job and compares its status, backend,
execution window, shots, CHSH counts, and, when IBM still returns them, the
hashes of the submitted circuits.

A pass shows that the jobs this token can see still match the evidence. It
is not hardware attestation: IBM is still trusted for the shots, and a
verifier who cannot see those jobs cannot run this check.
"""

from __future__ import annotations

import os
from typing import Any

from vqrng.backends.ibm import (
    CHANNEL,
    INSTANCE_ENV_VAR,
    circuit_sha256,
    execution_window_from_metrics,
    read_bitstrings,
    read_token,
    status_name,
)
from vqrng.evidence import CHSH_SETTINGS

POOL_REGISTER = "c"


def _service() -> Any:
    from qiskit_ibm_runtime import QiskitRuntimeService

    return QiskitRuntimeService(
        channel=CHANNEL, token=read_token(), instance=os.environ.get(INSTANCE_ENV_VAR) or None
    )


def _is_simulator(backend: Any) -> bool:
    if getattr(backend, "simulator", None) is True:
        return True
    try:
        return backend.configuration().simulator is True
    except Exception:
        return False


def _submitted_circuits(job: Any) -> list[Any]:
    pubs = list(job.inputs["pubs"])
    circuits = [pub.circuit if hasattr(pub, "circuit") else pub[0] for pub in pubs]
    if not circuits:
        raise LookupError("no pubs")
    return circuits


def _check_programs(job: Any, label: str, recorded: list[Any]) -> tuple[list[str], list[str]]:
    try:
        circuits = _submitted_circuits(job)
    except Exception:
        return [], [f"{label}: IBM did not return the submitted circuits, so their hashes were not compared."]
    errors: list[str] = []
    for index, want in enumerate(recorded):
        if want is None:
            continue
        got = circuit_sha256(circuits[index]) if index < len(circuits) else None
        if got != want:
            errors.append(f"{label}: submitted circuit {index} hashes to {got}, but the evidence records {want}.")
    return errors, []


def _chsh_for_job(evidence: dict, job_id: str) -> list[tuple[str, Any, Any]]:
    data = evidence.get("chsh_data")
    data = data if isinstance(data, dict) else {}
    runs = data.get("runs") if isinstance(data.get("runs"), dict) else {}
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    return [
        (setting, counts.get(setting), runs[setting].get("isa_sha256"))
        for setting in CHSH_SETTINGS
        if isinstance(runs.get(setting), dict) and runs[setting].get("job_id") == job_id
    ]


def _check_batch(service: Any, evidence: dict, position: int, batch: dict) -> tuple[list[str], list[str]]:
    from vqrng.quantum.chsh import tally_counts

    job_id = batch["job_id"]
    label = f"tape[{position}] job {job_id}"
    try:
        job = service.job(job_id)
    except Exception as exc:
        return [f"{label}: IBM returned no such job for this token ({exc})."], []

    errors: list[str] = []
    backend = job.backend()
    name = getattr(backend, "name", backend)
    if name != batch.get("backend"):
        errors.append(f"{label} ran on {name!r} according to IBM, but the evidence says {batch.get('backend')!r}.")
    if _is_simulator(backend):
        errors.append(f"{label} ran on the simulator {name!r}, not a QPU.")
    if not batch.get("bitstrings"):
        return errors, []  # Nothing was measured at generation time, so there is nothing to compare.

    status = status_name(job.status())
    if status != "DONE":
        return errors + [f"{label} has status {status} at IBM, expected DONE."], []
    window = execution_window_from_metrics(job.metrics() or {})
    recorded = (batch.get("started_at"), batch.get("finished_at"))
    if window != recorded:
        errors.append(
            f"{label} ran from {window[0]} to {window[1]} according to IBM, "
            f"but the evidence records {recorded[0]} to {recorded[1]}."
        )
    try:
        results = job.result()
        shots = read_bitstrings(results[0], POOL_REGISTER, evidence.get("n_bits"))
    except Exception as exc:
        return errors + [f"{label}: IBM's result could not be read ({exc})."], []
    if shots != batch["bitstrings"]:
        errors.append(f"{label}: the shots IBM returns differ from the tape.")

    chsh = _chsh_for_job(evidence, job_id)
    for index, (setting, counts, _) in enumerate(chsh, start=1):
        try:
            got = tally_counts(read_bitstrings(results[index], POOL_REGISTER, 2))
        except Exception as exc:
            errors.append(f"{label}: CHSH setting {setting} could not be read from IBM's result ({exc}).")
            continue
        if got != counts:
            errors.append(f"{label}: CHSH setting {setting} counts are {got} at IBM, but chsh_data records {counts}.")

    program_errors, notes = _check_programs(job, label, [batch.get("isa_sha256")] + [isa for _, _, isa in chsh])
    return errors + program_errors, notes


def check_ibm_jobs(evidence: Any, service: Any = None) -> tuple[list[str], list[str]]:
    """Compare ``evidence`` with the IBM jobs it names. Returns ``(errors, notes)``.

    ``service`` is a ``QiskitRuntimeService``; by default one is opened with
    ``IBMQ_API_TOKEN`` or ``QISKIT_IBM_TOKEN``.
    """
    if not isinstance(evidence, dict):
        return [f"Evidence must be an object, got {type(evidence).__name__}."], []
    tape = evidence.get("tape")
    batches = [
        (position, batch) for position, batch in enumerate(tape if isinstance(tape, list) else [])
        if isinstance(batch, dict) and batch.get("job_id") is not None
    ]
    if not batches:
        return [f"the evidence names no IBM jobs to check (mode is {evidence.get('mode')!r})."], []
    if service is None:
        try:
            service = _service()
        except Exception as exc:
            return [f"cannot connect to IBM Quantum: {exc}"], []

    errors: list[str] = []
    notes: list[str] = []
    for position, batch in batches:
        batch_errors, batch_notes = _check_batch(service, evidence, position, batch)
        errors.extend(batch_errors)
        notes.extend(batch_notes)
    return errors, notes
