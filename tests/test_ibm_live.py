"""vqrng verify --ibm: the evidence against the IBM jobs it names. IBM is faked; no network."""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import vqrng
from vqrng import cli
from vqrng.backends import BackendJobError, BackendRun, BaseBackend
from vqrng.backends.ibm import circuit_sha256
from vqrng.evidence import payload_hash
from vqrng.verifier import ibm_live
from vqrng.verifier.ibm_live import check_ibm_jobs
from tests.test_generate import fake_source

pytestmark = pytest.mark.usefixtures("passthrough_extractor")

STARTED, FINISHED = "2026-09-28T06:00:12.500000+00:00", "2026-09-28T06:00:14+00:00"
METRICS = {"timestamps": {"running": "2026-09-28T06:00:12.5Z", "finished": "2026-09-28T06:00:14Z"}}
CORRELATED, ANTI = ["00", "11", "00", "11"], ["01", "10", "01", "10"]
CHSH_SHOTS = [CORRELATED, CORRELATED, CORRELATED, ANTI]  # A0B0, A0B1, A1B0, A1B1: S = 4


def register(bits):
    return SimpleNamespace(get_bitstrings=lambda: list(bits))


class Qpu(BaseBackend):
    """Runs each batch as one job and remembers what it returned, so a fake IBM can serve it back."""

    def __init__(self, name="ibm_fez"):
        self.name = name
        self.jobs = {}
        self.pool = fake_source([1, 2, 3, 4, 5, 0])

    def run(self, circuit, shots, budget):
        raise AssertionError("run_many is used")

    def run_many(self, circuits, budget):
        job_id = f"job-{len(self.jobs) + 1}"
        runs = []
        for index, (circuit, shots) in enumerate(circuits):
            bits = self.pool(circuit, shots, budget)[0] if index == 0 else CHSH_SHOTS[index - 1]
            runs.append(BackendRun(bits, self.name, job_id=job_id, started_at=STARTED, finished_at=FINISHED,
                                   isa_sha256=circuit_sha256(circuit)))
        self.jobs[job_id] = fake_job(self.name, [run.bitstrings for run in runs], [c for c, _ in circuits])
        return runs


def fake_job(backend, results, circuits, status="DONE", metrics=METRICS):
    job = MagicMock(name="job")
    job.backend.return_value = SimpleNamespace(name=backend)
    job.status.return_value = status
    job.metrics.return_value = metrics
    job.result.return_value = [SimpleNamespace(data={"c": register(bits)}) for bits in results]
    job.inputs = {"pubs": [(circuit, None, 8) for circuit in circuits]}
    return job


def service_for(jobs):
    def job(job_id):
        if job_id not in jobs:
            raise KeyError(f"job {job_id} not found")
        return jobs[job_id]

    return SimpleNamespace(job=job)


@pytest.fixture
def qpu():
    return Qpu()


@pytest.fixture
def evidence(qpu):
    return vqrng.generate(1, 6, pool_size=2, mode="hardware", runtime_limit=60, backend=qpu, chsh=True, chsh_shots=4)


class TestMatch:
    def test_matching_jobs_pass(self, qpu, evidence):
        assert check_ibm_jobs(evidence, service_for(qpu.jobs)) == ([], [])
        result = vqrng.verify(evidence, ibm=True, ibm_service=service_for(qpu.jobs))
        assert result.is_valid, result.errors
        assert result.levels["IBM"].passed and result.levels["IBM"].name == "live IBM job check"

    def test_the_tape_and_chsh_runs_record_the_submitted_circuit_hashes(self, evidence):
        assert len(evidence["tape"][0]["isa_sha256"]) == 64
        assert all(len(run["isa_sha256"]) == 64 for run in evidence["chsh_data"]["runs"].values())

    def test_the_check_is_absent_unless_requested(self, evidence):
        assert "IBM" not in vqrng.verify(evidence).levels

    def test_pubs_given_as_objects_are_read_by_circuit(self, qpu, evidence):
        job = qpu.jobs["job-1"]
        job.inputs = {"pubs": [SimpleNamespace(circuit=pub[0]) for pub in job.inputs["pubs"]]}
        assert check_ibm_jobs(evidence, service_for(qpu.jobs)) == ([], [])

    def test_an_unrecorded_hash_is_not_compared(self, qpu, evidence):
        evidence["tape"][0]["isa_sha256"] = None
        qpu.jobs["job-1"].inputs = {"pubs": [("other", None, 8)] + qpu.jobs["job-1"].inputs["pubs"][1:]}
        assert check_ibm_jobs(evidence, service_for(qpu.jobs)) == ([], [])


class TestMismatch:
    def check(self, qpu, evidence):
        errors, _ = check_ibm_jobs(evidence, service_for(qpu.jobs))
        return errors

    def test_a_missing_job_fails(self, evidence):
        assert self.check(SimpleNamespace(jobs={}), evidence) == [
            "tape[0] job job-1: IBM returned no such job for this token ('job job-1 not found')."
        ]

    def test_another_backend_fails(self, qpu, evidence):
        qpu.jobs["job-1"].backend.return_value = SimpleNamespace(name="ibm_torino")
        assert self.check(qpu, evidence) == [
            "tape[0] job job-1 ran on 'ibm_torino' according to IBM, but the evidence says 'ibm_fez'."
        ]

    @pytest.mark.parametrize("backend", [
        SimpleNamespace(name="ibm_fez", simulator=True),
        SimpleNamespace(name="ibm_fez", configuration=lambda: SimpleNamespace(simulator=True)),
    ])
    def test_a_simulator_fails(self, qpu, evidence, backend):
        qpu.jobs["job-1"].backend.return_value = backend
        assert self.check(qpu, evidence) == ["tape[0] job job-1 ran on the simulator 'ibm_fez', not a QPU."]

    def test_a_job_that_is_not_done_fails(self, qpu, evidence):
        qpu.jobs["job-1"].status.return_value = "CANCELLED"
        assert self.check(qpu, evidence) == ["tape[0] job job-1 has status CANCELLED at IBM, expected DONE."]

    def test_another_execution_window_fails(self, qpu, evidence):
        qpu.jobs["job-1"].metrics.return_value = {"timestamps": {"running": "2026-09-28T07:00:00Z"}}
        assert self.check(qpu, evidence) == [
            "tape[0] job job-1 ran from 2026-09-28T07:00:00+00:00 to None according to IBM, "
            f"but the evidence records {STARTED} to {FINISHED}."
        ]

    def test_missing_metrics_count_as_no_window(self, qpu, evidence):
        qpu.jobs["job-1"].metrics.return_value = None
        assert self.check(qpu, evidence)[0].startswith("tape[0] job job-1 ran from None to None")

    def test_edited_shots_fail(self, qpu, evidence):
        evidence["tape"][0]["bitstrings"][0] = "110"
        assert self.check(qpu, evidence) == ["tape[0] job job-1: the shots IBM returns differ from the tape."]

    def test_an_unreadable_result_fails(self, qpu, evidence):
        qpu.jobs["job-1"].result.side_effect = RuntimeError("expired")
        assert self.check(qpu, evidence) == ["tape[0] job job-1: IBM's result could not be read (expired)."]

    def test_edited_chsh_counts_fail(self, qpu, evidence):
        evidence["chsh_data"]["counts"]["A1B1"]["00"] += 1
        (error,) = self.check(qpu, evidence)
        assert error.startswith("tape[0] job job-1: CHSH setting A1B1 counts are {")
        assert "but chsh_data records {'00': 1, '01': 2, '10': 2, '11': 0}." in error

    def test_a_missing_chsh_result_fails(self, qpu, evidence):
        results = qpu.jobs["job-1"].result.return_value
        qpu.jobs["job-1"].result.return_value = results[:4]
        (error,) = self.check(qpu, evidence)
        assert error.startswith("tape[0] job job-1: CHSH setting A1B1 could not be read from IBM's result (")

    def test_another_submitted_circuit_fails(self, qpu, evidence):
        pubs = qpu.jobs["job-1"].inputs["pubs"]
        pubs[1] = (pubs[0][0], None, 8)
        (error,) = self.check(qpu, evidence)
        recorded = evidence["chsh_data"]["runs"]["A0B0"]["isa_sha256"]
        assert error == (
            f"tape[0] job job-1: submitted circuit 1 hashes to {circuit_sha256(pubs[0][0])}, "
            f"but the evidence records {recorded}."
        )

    def test_fewer_submitted_circuits_than_recorded_fails(self, qpu, evidence):
        qpu.jobs["job-1"].inputs = {"pubs": qpu.jobs["job-1"].inputs["pubs"][:1]}
        errors = self.check(qpu, evidence)
        assert len(errors) == 4 and all("hashes to None" in error for error in errors)

    @pytest.mark.parametrize("inputs", [{}, {"pubs": []}, None])
    def test_circuits_ibm_no_longer_returns_are_a_note(self, qpu, evidence, inputs):
        qpu.jobs["job-1"].inputs = inputs
        assert check_ibm_jobs(evidence, service_for(qpu.jobs)) == ([], [
            "tape[0] job job-1: IBM did not return the submitted circuits, so their hashes were not compared."
        ])


class TestScope:
    def test_simulator_evidence_names_no_jobs(self):
        evidence = vqrng.generate(1, 6, _source=fake_source([1]))
        assert check_ibm_jobs(evidence, service_for({})) == (
            ["the evidence names no IBM jobs to check (mode is 'aer')."], []
        )

    @pytest.mark.parametrize("evidence", [[], {"tape": "none"}])
    def test_malformed_evidence(self, evidence):
        errors, _ = check_ibm_jobs(evidence, service_for({}))
        assert errors[0].startswith(("Evidence must be an object", "the evidence names no IBM jobs"))

    def test_a_job_that_failed_at_generation_only_checks_its_backend(self):
        class Failing(BaseBackend):
            def run(self, circuit, shots, budget):
                raise BackendJobError("job-9 ended with ERROR", job_id="job-9", backend_name="ibm_fez")

        with pytest.raises(vqrng.GenerationError) as exc:
            vqrng.generate(1, 6, mode="hardware", runtime_limit=60, backend=Failing())
        job = fake_job("ibm_fez", [], [], status="ERROR")
        assert check_ibm_jobs(exc.value.evidence, service_for({"job-9": job})) == ([], [])
        job.status.assert_not_called()


class TestService:
    def test_the_token_in_the_environment_opens_the_service(self, qpu, evidence, monkeypatch):
        monkeypatch.setenv("IBMQ_API_TOKEN", "token-1")
        monkeypatch.setenv("QISKIT_IBM_INSTANCE", "crn:1")
        service_cls = MagicMock(return_value=service_for(qpu.jobs))
        with patch("qiskit_ibm_runtime.QiskitRuntimeService", service_cls):
            assert check_ibm_jobs(evidence) == ([], [])
        service_cls.assert_called_once_with(channel="ibm_quantum_platform", token="token-1", instance="crn:1")

    def test_no_token_is_a_clear_failure(self, evidence, monkeypatch):
        monkeypatch.delenv("IBMQ_API_TOKEN", raising=False)
        monkeypatch.delenv("QISKIT_IBM_TOKEN", raising=False)
        errors, _ = check_ibm_jobs(evidence)
        assert errors == [
            "cannot connect to IBM Quantum: IBM Quantum hardware needs an API token. "
            "Set IBMQ_API_TOKEN or QISKIT_IBM_TOKEN in your environment."
        ]


class TestCli:
    def run(self, capsys, monkeypatch, evidence, *flags):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(evidence)))
        code = cli.main(["verify", *flags])
        return code, capsys.readouterr().out

    def test_ibm_pass_line(self, qpu, evidence, capsys, monkeypatch):
        monkeypatch.setattr(ibm_live, "_service", lambda: service_for(qpu.jobs))
        code, out = self.run(capsys, monkeypatch, evidence, "--ibm")
        assert code == 0
        assert out.splitlines()[-1] == (
            "PASS: live IBM job check (this token's jobs match the evidence; not hardware attestation)"
        )

    def test_ibm_failure_and_notes(self, qpu, evidence, capsys, monkeypatch):
        qpu.jobs["job-1"].status.return_value = "ERROR"
        qpu.jobs["job-1"].inputs = None
        monkeypatch.setattr(ibm_live, "_service", lambda: service_for(qpu.jobs))
        code, out = self.run(capsys, monkeypatch, evidence, "--ibm")
        assert code == 1
        assert "FAIL: live IBM job check\n  - tape[0] job job-1 has status ERROR at IBM, expected DONE.\n" in out

    def test_notes_are_printed(self, qpu, evidence, capsys, monkeypatch):
        qpu.jobs["job-1"].inputs = None
        monkeypatch.setattr(ibm_live, "_service", lambda: service_for(qpu.jobs))
        code, out = self.run(capsys, monkeypatch, evidence, "--ibm")
        assert code == 0
        assert "NOTE: tape[0] job job-1: IBM did not return the submitted circuits" in out

    def test_offline_verify_never_opens_a_service(self, evidence, capsys, monkeypatch):
        monkeypatch.setattr(ibm_live, "_service", MagicMock(side_effect=AssertionError("network")))
        code, out = self.run(capsys, monkeypatch, evidence)
        assert code == 0 and "IBM" not in out


class TestRequireChsh:
    @pytest.fixture
    def plain(self):
        return vqrng.generate(1, 6, _source=fake_source([1]))

    def test_a_skip_still_passes_by_default(self, plain):
        result = vqrng.verify(plain)
        assert result.is_valid and result.levels["C"].status == "skipped"

    def test_required_chsh_fails_when_absent(self, plain):
        result = vqrng.verify(plain, require_chsh=True)
        assert not result.is_valid
        assert result.levels["C"].errors == ["chsh_data is missing, but a CHSH spot-check is required."]

    def test_required_chsh_passes_when_present(self, evidence):
        assert vqrng.verify(evidence, require_chsh=True).levels["C"].status != "skipped"

    def test_invalid_json_fails_every_requested_check(self):
        result = vqrng.verify("{", require_chsh=True, ibm=True)
        assert [level.status for level in result.levels.values()] == ["fail"] * 4
        assert len(result.errors) == 1

    def test_invalid_json_without_requirements_skips_c(self):
        result = vqrng.verify("{")
        assert result.levels["C"].status == "skipped" and "IBM" not in result.levels

    def test_cli_flag(self, plain, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plain)))
        code = cli.main(["verify", "--require-chsh"])
        out = capsys.readouterr().out
        assert code == 1
        assert "Level C (near-real-time CHSH spot-check): FAILED\n" in out
        assert "  - chsh_data is missing, but a CHSH spot-check is required." in out

    def test_help_lists_the_new_flags(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["verify", "--help"])
        out = capsys.readouterr().out
        assert "--require-chsh" in out and "--ibm" in out and "not hardware attestation" in out


def test_resealed_evidence_still_differs_from_ibm(qpu, evidence):
    evidence["tape"][0]["bitstrings"][1] = "000"
    evidence["pool_hash"] = payload_hash(evidence)
    result = vqrng.verify(evidence, ibm=True, ibm_service=service_for(qpu.jobs))
    assert "Level IBM: tape[0] job job-1: the shots IBM returns differ from the tape." in result.errors
