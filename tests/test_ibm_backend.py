"""IBM Quantum backend tests. Qiskit Runtime is mocked, so no token or network is needed."""

import json
from enum import Enum
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import vqrng
from vqrng import cli
from vqrng.backends import BackendRun, IBMBackend
from vqrng.backends import ibm as ibm_module
from vqrng.core import build_circuit

CREATED = "2026-09-28T06:00:00Z"
RUNNING = "2026-09-28T06:00:12.500000Z"


def make_job(bitstrings, statuses=("QUEUED", "RUNNING", "DONE"), metrics=None, job_id="job-1", error=None):
    job = MagicMock(name=job_id)
    job.job_id.return_value = job_id
    job.status.side_effect = list(statuses)
    if metrics is None:
        metrics = {
            "usage": {"quantum_seconds": 1.5},
            "timestamps": {"created": CREATED, "running": RUNNING},
        }
    job.metrics.return_value = metrics
    job.error_message.return_value = error
    register = MagicMock()
    register.get_bitstrings.return_value = list(bitstrings)
    job.result.return_value = [SimpleNamespace(data={"c": register})]
    return job


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def ibm(monkeypatch):
    monkeypatch.setenv("IBMQ_API_TOKEN", "test-token")
    monkeypatch.delenv("QISKIT_IBM_TOKEN", raising=False)
    monkeypatch.delenv("QISKIT_IBM_INSTANCE", raising=False)
    monkeypatch.setattr(ibm_module, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(ibm_module, "USAGE_RETRY_SECONDS", 0.0)

    qpu = SimpleNamespace(name="ibm_sherbrooke")
    service_cls = MagicMock(name="QiskitRuntimeService")
    service_cls.return_value.least_busy.return_value = qpu
    service_cls.return_value.backend.side_effect = lambda name: SimpleNamespace(name=name)

    sampler_cls = MagicMock(name="SamplerV2")
    sampler = sampler_cls.return_value
    sampler.options = SimpleNamespace()
    jobs = []
    sampler.run.side_effect = lambda circuits, shots: jobs.pop(0)

    pass_manager = MagicMock(name="generate_preset_pass_manager")
    pass_manager.return_value.run.side_effect = lambda circuit: circuit

    with patch("qiskit_ibm_runtime.QiskitRuntimeService", service_cls), \
            patch("qiskit_ibm_runtime.SamplerV2", sampler_cls), \
            patch("qiskit.transpiler.preset_passmanagers.generate_preset_pass_manager", pass_manager):
        yield SimpleNamespace(service=service_cls, sampler=sampler, jobs=jobs, qpu=qpu)


class TestToken:
    def test_ibmq_api_token_is_checked_first(self):
        env = {"IBMQ_API_TOKEN": "first", "QISKIT_IBM_TOKEN": "second"}
        assert ibm_module.read_token(env) == "first"

    @pytest.mark.parametrize("ibmq", [None, "", "   "])
    def test_falls_back_to_qiskit_ibm_token(self, ibmq):
        env = {"QISKIT_IBM_TOKEN": " second "}
        if ibmq is not None:
            env["IBMQ_API_TOKEN"] = ibmq
        assert ibm_module.read_token(env) == "second"

    def test_missing_token_is_a_clear_value_error(self, monkeypatch):
        monkeypatch.delenv("IBMQ_API_TOKEN", raising=False)
        monkeypatch.delenv("QISKIT_IBM_TOKEN", raising=False)
        with pytest.raises(ValueError, match="IBMQ_API_TOKEN or QISKIT_IBM_TOKEN"):
            IBMBackend()

    def test_generate_rejects_hardware_without_a_token_before_contacting_ibm(self, ibm, monkeypatch):
        monkeypatch.delenv("IBMQ_API_TOKEN")
        with pytest.raises(ValueError, match="API token"):
            vqrng.generate(1, 100, mode="hardware", runtime_limit=300)
        ibm.service.assert_not_called()

    def test_service_uses_the_platform_channel_token_and_instance(self, ibm, monkeypatch):
        monkeypatch.setenv("QISKIT_IBM_INSTANCE", "crn:v1:test")
        ibm.jobs.append(make_job(["0"]))
        IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 10)
        ibm.service.assert_called_once_with(
            channel="ibm_quantum_platform", token="test-token", instance="crn:v1:test"
        )

    def test_token_never_appears_in_the_log(self, ibm):
        messages = []
        ibm.jobs.append(make_job(["0"]))
        IBMBackend(log=messages.append).run(build_circuit(1), 8, 10)
        assert messages
        assert not any("test-token" in message for message in messages)


class TestBudgetValidation:
    def test_generate_requires_runtime_limit_in_hardware_mode(self, ibm):
        with pytest.raises(ValueError, match="runtime_limit is required"):
            vqrng.generate(1, 100, mode="hardware")
        ibm.service.assert_not_called()

    @pytest.mark.parametrize("budget", [300, 2.9, 0.4, None])
    def test_budget_is_not_sent_as_an_ibm_execution_limit(self, ibm, budget):
        ibm.jobs.append(make_job(["0"]))
        IBMBackend(log=lambda message: None).run(build_circuit(1), 8, budget)
        assert not hasattr(ibm.sampler.options, "max_execution_time")

    def test_later_batches_stop_on_reported_qpu_time(self, ibm):
        rejected = make_job(["1111111"] * 8, metrics={"usage": {"quantum_seconds": 100}}, job_id="job-1")
        accepted = make_job(["0000101"], metrics={"usage": {"quantum_seconds": 20}}, job_id="job-2")
        ibm.jobs.extend([rejected, accepted])
        evidence = vqrng.generate(1, 100, mode="hardware", runtime_limit=300)
        assert not hasattr(ibm.sampler.options, "max_execution_time")
        assert evidence["quantum_seconds"] == 120.0
        assert evidence["job_ids"] == ["job-1", "job-2"]
        assert evidence["items"][0]["rejected"] == ["1111111"] * 8

    def test_budget_exhausted_by_qpu_time_stops_further_jobs(self, ibm):
        ibm.jobs.append(make_job(["1111111"] * 8, metrics={"usage": {"quantum_seconds": 300}}))
        with pytest.raises(RuntimeError, match="budget of 300s exhausted after 0/1"):
            vqrng.generate(1, 100, mode="hardware", runtime_limit=300)
        assert ibm.sampler.run.call_count == 1


class TestBackendSelection:
    def test_least_busy_operational_hardware_is_the_default(self, ibm):
        ibm.jobs.append(make_job(["0" * 7]))
        run = IBMBackend(log=lambda message: None).run(build_circuit(7), 8, 10)
        ibm.service.return_value.least_busy.assert_called_once_with(
            operational=True, simulator=False, min_num_qubits=7
        )
        assert run.backend_name == "ibm_sherbrooke"

    def test_explicit_backend_name(self, ibm):
        ibm.jobs.append(make_job(["0"]))
        run = IBMBackend("ibm_torino", log=lambda message: None).run(build_circuit(1), 8, 10)
        ibm.service.return_value.backend.assert_called_once_with("ibm_torino")
        ibm.service.return_value.least_busy.assert_not_called()
        assert run.backend_name == "ibm_torino"

    def test_backend_is_selected_once_across_batches(self, ibm):
        ibm.jobs.extend([make_job(["1"]), make_job(["0"], job_id="job-2")])
        backend = IBMBackend(log=lambda message: None)
        backend.run(build_circuit(1), 8, 10)
        backend.run(build_circuit(1), 8, 10)
        ibm.service.assert_called_once()
        ibm.service.return_value.least_busy.assert_called_once()

    def test_generate_passes_the_backend_name_through(self, ibm):
        ibm.jobs.append(make_job(["0000101"]))
        evidence = vqrng.generate(1, 100, mode="hardware", runtime_limit=300, backend="ibm_kyiv")
        assert evidence["backend"] == "ibm_kyiv"
        assert evidence["request"]["backend"] == "ibm_kyiv"

    def test_backend_name_is_hardware_only(self):
        with pytest.raises(ValueError, match="hardware mode"):
            vqrng.generate(1, 100, backend="ibm_kyiv")


class TestPolling:
    def test_status_changes_are_logged_in_order(self, ibm):
        messages = []
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED", "QUEUED", "RUNNING", "RUNNING", "DONE"]))
        IBMBackend(log=messages.append).run(build_circuit(1), 8, 10)
        statuses = [m.split()[-1] for m in messages if m.startswith("job job-1 ") and len(m.split()) == 3]
        assert statuses == ["QUEUED", "RUNNING", "DONE"]
        assert "selected IBM backend ibm_sherbrooke" in messages[0]
        assert "submitted to ibm_sherbrooke (8 shots)" in messages[1]

    def test_long_queue_reports_a_heartbeat(self, ibm):
        clock = FakeClock()
        messages = []
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED"] * 8 + ["DONE"]))
        IBMBackend(log=messages.append, poll_interval=10, clock=clock, sleep=clock.sleep).run(
            build_circuit(1), 8, 10
        )
        assert any("still QUEUED (60s elapsed)" in message for message in messages)

    @pytest.mark.parametrize(("raw", "name"), [
        ("done", "DONE"), ("COMPLETED", "DONE"), ("FAILED", "ERROR"), ("Canceled", "CANCELLED"),
    ])
    def test_status_names_are_normalised(self, raw, name):
        assert ibm_module.status_name(raw) == name

    def test_enum_statuses_are_normalised(self):
        class JobStatus(Enum):
            RUNNING = "job is actively running"

        assert ibm_module.status_name(JobStatus.RUNNING) == "RUNNING"

    def test_error_status_raises_with_the_ibm_message(self, ibm):
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED", "ERROR"], error="Qubit calibration failed"))
        with pytest.raises(RuntimeError, match="ended with status ERROR: Qubit calibration failed"):
            IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 10)

    def test_cancelled_status_raises(self, ibm):
        job = make_job(["0"], statuses=["QUEUED", "CANCELLED"])
        ibm.jobs.append(job)
        with pytest.raises(RuntimeError, match=r"job-1 on ibm_sherbrooke ended with status CANCELLED$"):
            IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 10)
        job.result.assert_not_called()

    def test_interrupt_while_waiting_cancels_the_job(self, ibm):
        job = make_job(["0"])
        job.status.side_effect = ["QUEUED", KeyboardInterrupt]
        ibm.jobs.append(job)
        messages = []
        with pytest.raises(KeyboardInterrupt):
            IBMBackend(log=messages.append).run(build_circuit(1), 8, 10)
        job.cancel.assert_called_once()
        assert "cancelling" in messages[-1]


class TestTiming:
    def test_qpu_time_and_queue_time_come_from_job_metrics(self, ibm):
        ibm.jobs.append(make_job(["0"]))
        run = IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 10)
        assert run == BackendRun(["0"], "ibm_sherbrooke", 1.5, 12.5, "job-1")

    def test_charge_time_is_used_when_quantum_seconds_is_absent(self):
        assert ibm_module.quantum_seconds_from_metrics({"usage": {"qpu_charge_time_seconds": 3}}) == 3.0

    @pytest.mark.parametrize("metrics", [{}, {"usage": None}, {"usage": {"quantum_seconds": True}}])
    def test_missing_usage_is_unknown(self, metrics):
        assert ibm_module.quantum_seconds_from_metrics(metrics) is None

    @pytest.mark.parametrize("timestamps", [
        None, {"created": CREATED}, {"created": 5, "running": RUNNING}, {"created": "soon", "running": RUNNING},
    ])
    def test_unusable_timestamps_give_no_queue_time(self, timestamps):
        assert ibm_module.queue_seconds_from_metrics({"timestamps": timestamps}) is None

    def test_queue_time_falls_back_to_when_running_was_first_seen(self, ibm):
        clock = FakeClock()
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED", "QUEUED", "RUNNING", "DONE"],
                                 metrics={"usage": {"quantum_seconds": 2}}))
        run = IBMBackend(log=lambda m: None, poll_interval=10, clock=clock, sleep=clock.sleep).run(
            build_circuit(1), 8, 10
        )
        assert run.queue_seconds == 20.0
        assert run.quantum_seconds == 2.0

    def test_queue_time_falls_back_to_finish_when_running_was_never_seen(self, ibm):
        clock = FakeClock()
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED", "DONE"], metrics={}))
        run = IBMBackend(log=lambda m: None, poll_interval=10, clock=clock, sleep=clock.sleep).run(
            build_circuit(1), 8, 10
        )
        assert run.queue_seconds == 10.0

    def test_pending_usage_is_retried_until_final(self, ibm):
        job = make_job(["0"])
        job.metrics.side_effect = [
            {"usage": {"status": "pending"}},
            None,
        ]
        ibm.jobs.append(job)
        run = IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 10)
        assert job.metrics.call_count == 2
        assert run.quantum_seconds == 0.0

    def test_usage_still_pending_is_recorded_with_a_warning(self, ibm, monkeypatch):
        sleeps = []
        monkeypatch.setattr(ibm_module, "USAGE_ATTEMPTS", 3)
        monkeypatch.setattr(ibm_module, "USAGE_RETRY_SECONDS", 0.25)
        job = make_job(["0"], metrics={"usage": {"status": "pending", "quantum_seconds": 0.7}})
        ibm.jobs.append(job)
        messages = []
        run = IBMBackend(log=messages.append, sleep=sleeps.append).run(build_circuit(1), 8, 10)
        assert job.metrics.call_count == 3
        assert sleeps.count(ibm_module.USAGE_RETRY_SECONDS) == 2
        assert run.quantum_seconds == 0.7
        assert any("not finalised" in message for message in messages)

    def test_metrics_failure_keeps_the_measured_bits(self, ibm):
        job = make_job(["0"])
        job.metrics.side_effect = ConnectionError("metadata unavailable")
        ibm.jobs.append(job)
        messages = []
        run = IBMBackend(log=messages.append).run(build_circuit(1), 8, 10)
        assert run.bitstrings == ["0"]
        assert run.quantum_seconds == 0.0
        assert any("metadata unavailable" in message for message in messages)

    def test_stderr_log_is_timestamped(self, capsys):
        ibm_module.stderr_log("job job-1 QUEUED")
        out, err = capsys.readouterr()
        assert out == ""
        assert err.startswith("vqrng: [")
        assert err.endswith("] job job-1 QUEUED\n")


class TestPessimisticBudget:
    def charged(self, ibm, job, budget=10):
        ibm.jobs.append(job)
        return IBMBackend(log=lambda message: None).run(build_circuit(1), 8, budget)

    def test_final_usage_is_charged_as_reported(self, ibm):
        run = self.charged(ibm, make_job(["0"]))
        assert run.charged_seconds is None
        assert run.quantum_seconds == 1.5

    def test_unreadable_metrics_charge_the_full_limit(self, ibm):
        job = make_job(["0"])
        job.metrics.side_effect = ConnectionError("metadata unavailable")
        run = self.charged(ibm, job)
        assert (run.quantum_seconds, run.charged_seconds) == (0.0, 10.0)

    def test_pending_usage_charges_the_full_limit(self, ibm, monkeypatch):
        monkeypatch.setattr(ibm_module, "USAGE_ATTEMPTS", 2)
        run = self.charged(ibm, make_job(["0"], metrics={"usage": {"status": "pending", "quantum_seconds": 0.7}}))
        assert (run.quantum_seconds, run.charged_seconds) == (0.7, 10.0)

    def test_final_usage_without_seconds_charges_the_full_limit(self, ibm):
        run = self.charged(ibm, make_job(["0"], metrics={"usage": {"status": "completed"}}))
        assert run.charged_seconds == 10.0

    def test_unknown_usage_without_a_limit_is_not_estimated(self, ibm):
        run = self.charged(ibm, make_job(["0"], metrics={}), budget=None)
        assert run.charged_seconds is None

    def test_unknown_usage_stops_the_next_batch_from_reusing_the_budget(self, ibm):
        job = make_job(["1111111"] * 8)
        job.metrics.side_effect = ConnectionError("metadata unavailable")
        ibm.jobs.append(job)
        with pytest.raises(vqrng.GenerationError, match="budget of 300s exhausted after 0/1") as exc:
            vqrng.generate(1, 100, mode="hardware", runtime_limit=300)
        assert ibm.sampler.run.call_count == 1
        evidence = exc.value.evidence
        assert evidence["quantum_seconds"] == 0.0
        assert evidence["charged_seconds"] == 300.0
        assert evidence["job_ids"] == ["job-1"]
        assert evidence["tape"][0]["bitstrings"] == ["1111111"] * 8

    def test_failed_job_charges_its_limit(self, ibm):
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED", "ERROR"]))
        with pytest.raises(vqrng.BackendJobError) as exc:
            IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 42.7)
        assert (exc.value.job_id, exc.value.backend_name, exc.value.charged_seconds) == (
            "job-1", "ibm_sherbrooke", 42.7,
        )

    def test_failed_job_without_a_limit_charges_nothing(self, ibm):
        ibm.jobs.append(make_job(["0"], statuses=["CANCELLED"]))
        with pytest.raises(vqrng.BackendJobError) as exc:
            IBMBackend(log=lambda message: None).run(build_circuit(1), 8, None)
        assert exc.value.charged_seconds == 0.0


class TestPartialRecovery:
    def test_a_failed_second_job_keeps_the_first_jobs_values(self, ibm):
        # [1, 100] -> 7 bits. Job 1 yields 6 and a pending reject; job 2 errors.
        ibm.jobs.extend([
            make_job(["0000101", "1111111"], job_id="job-1"),
            make_job(["0"], statuses=["QUEUED", "ERROR"], error="backend offline", job_id="job-2"),
        ])
        with pytest.raises(vqrng.GenerationError, match="job-2 .* ERROR: backend offline") as exc:
            vqrng.generate(1, 100, mode="hardware", runtime_limit=300, pool_size=2)
        evidence = exc.value.evidence
        assert evidence["status"] == "partial"
        assert "backend offline" in evidence["error"]
        assert [item["number"] for item in evidence["items"]] == [6]
        assert evidence["job_ids"] == ["job-1", "job-2"]
        assert evidence["tape"][0]["bitstrings"] == ["0000101", "1111111"]
        assert evidence["tape"][1]["bitstrings"] == []
        assert "backend offline" in evidence["tape"][1]["error"]
        assert evidence["charged_seconds"] == 1.5 + 298.5
        result = vqrng.verify(evidence)
        assert result.is_valid, result.errors
        assert result.evidence_status == "partial"

    def test_lost_polling_cancels_the_job_and_keeps_its_id(self, ibm):
        job = make_job(["0"])
        job.status.side_effect = ["QUEUED", ConnectionError("network down")]
        ibm.jobs.append(job)
        messages = []
        with pytest.raises(vqrng.BackendJobError, match="could not be polled: network down") as exc:
            IBMBackend(log=messages.append).run(build_circuit(1), 8, 10)
        job.cancel.assert_called_once()
        assert exc.value.job_id == "job-1"
        assert any("lost track" in message for message in messages)

    def test_a_failed_cancel_is_logged_and_the_original_error_kept(self, ibm):
        job = make_job(["0"])
        job.status.side_effect = ConnectionError("network down")
        job.cancel.side_effect = ConnectionError("still down")
        ibm.jobs.append(job)
        messages = []
        with pytest.raises(vqrng.BackendJobError, match="network down"):
            IBMBackend(log=messages.append).run(build_circuit(1), 8, 10)
        assert any("cancel failed (still down)" in message for message in messages)

    def test_unreadable_result_keeps_the_job_id(self, ibm):
        job = make_job(["0"])
        job.result.side_effect = ValueError("bad payload")
        ibm.jobs.append(job)
        with pytest.raises(vqrng.BackendJobError, match="unreadable result: bad payload") as exc:
            IBMBackend(log=lambda message: None).run(build_circuit(1), 8, 10)
        assert exc.value.job_id == "job-1"


def register(bitstrings):
    reg = MagicMock()
    reg.get_bitstrings.return_value = list(bitstrings)
    return reg


class TestResultNormalization:
    def test_dropped_leading_zeros_are_restored(self, ibm):
        ibm.jobs.append(make_job(["101", "0", "1111111"]))
        run = IBMBackend(log=lambda message: None).run(build_circuit(7), 8, 10)
        assert run.bitstrings == ["0000101", "0000000", "1111111"]

    @pytest.mark.parametrize("bits", ["11111111", "10x", "", 5])
    def test_malformed_shots_fail_the_job(self, ibm, bits):
        ibm.jobs.append(make_job([bits]))
        with pytest.raises(vqrng.BackendJobError, match="unreadable result: shot 0"):
            IBMBackend(log=lambda message: None).run(build_circuit(7), 8, 10)

    def test_register_by_attribute(self):
        data = SimpleNamespace(c=register(["1"]))
        assert ibm_module.extract_bitstrings(SimpleNamespace(data=data), build_circuit(2)) == ["01"]

    def test_register_under_another_name_when_it_is_the_only_one(self):
        data = {"meas": register(["10"])}
        assert ibm_module.extract_bitstrings(SimpleNamespace(data=data), build_circuit(2)) == ["10"]

    @pytest.mark.parametrize("data", [
        {}, {"a": register(["0"]), "b": register(["1"])}, SimpleNamespace(), {"c": "not a register"},
    ])
    def test_missing_register(self, data):
        with pytest.raises(ValueError, match="no classical register named 'c'"):
            ibm_module.extract_bitstrings(SimpleNamespace(data=data), build_circuit(1))


class TestRejectionSamplingOnHardware:
    def test_explicit_range(self, ibm):
        # [1, 100] -> 7 bits; 127 and 100 are rejected, 5 maps to 6 and 99 maps to 100.
        ibm.jobs.append(make_job(["1111111", "1100100", "0000101", "1100011"]))
        evidence = vqrng.generate(1, 100, mode="hardware", runtime_limit=300, pool_size=2)
        assert [item["number"] for item in evidence["items"]] == [6, 100]
        assert evidence["items"][0]["rejected"] == ["1111111", "1100100"]
        assert evidence["mode"] == "hardware"
        assert evidence["backend"] == "ibm_sherbrooke"
        assert evidence["quantum_seconds"] == 1.5
        assert evidence["queue_seconds"] == 12.5
        assert evidence["wall_seconds"] >= 0
        assert evidence["job_ids"] == ["job-1"]
        assert evidence["circuit"]["qasm"].startswith("OPENQASM 2.0;")
        assert vqrng.verify(evidence).is_valid

    def test_padded_digits(self, ibm):
        # -d 6 --pad -> [0, 999999], 20 bits. 2**20 - 1 is rejected.
        ibm.jobs.append(make_job(["1" * 20, format(4819, "020b")]))
        evidence = vqrng.generate(digits=6, pad=True, mode="hardware", runtime_limit=300)
        assert evidence["items"][0]["formatted"] == "004819"
        assert evidence["items"][0]["rejected"] == ["1" * 20]
        assert vqrng.verify(evidence).is_valid

    def test_unpadded_digits(self, ibm):
        ibm.jobs.append(make_job([format(0, "020b")]))
        evidence = vqrng.generate(digits=6, mode="hardware", runtime_limit=300)
        assert evidence["items"][0]["number"] == 100000
        assert vqrng.verify(evidence).is_valid


class TestCli:
    def run(self, capsys, *argv):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    def test_numbers_on_stdout_and_status_on_stderr(self, ibm, capsys):
        ibm.jobs.append(make_job(["1111111", "0000101"]))
        code, out, err = self.run(capsys, "-h", "-t", "300", "1", "100")
        assert code == 0
        assert out == "6\n"
        assert "warning: -t 300 does not cap what IBM bills" in err
        assert "job job-1 QUEUED" in err
        assert "job job-1 DONE" in err

    def test_digits_otp_on_hardware(self, ibm, capsys):
        ibm.jobs.append(make_job([format(4819, "020b")]))
        code, out, _ = self.run(capsys, "-h", "-t", "300", "-d", "6", "--pad")
        assert code == 0
        assert out == "004819\n"

    def test_json_evidence(self, ibm, capsys):
        ibm.jobs.append(make_job(["0000101"]))
        code, out, _ = self.run(capsys, "-h", "-t", "300", "-j", "1", "100")
        evidence = json.loads(out)
        assert code == 0
        assert evidence["mode"] == "hardware"
        assert evidence["backend"] == "ibm_sherbrooke"
        assert evidence["quantum_seconds"] == 1.5

    def test_backend_flag(self, ibm, capsys):
        ibm.jobs.append(make_job(["0000101"]))
        code, _, _ = self.run(capsys, "-h", "-t", "300", "--backend", "ibm_torino", "1", "100")
        assert code == 0
        ibm.service.return_value.backend.assert_called_once_with("ibm_torino")

    def test_backend_flag_requires_hardware(self, capsys):
        code, out, err = self.run(capsys, "--backend", "ibm_torino", "1", "100")
        assert code == 1
        assert out == ""
        assert "--backend requires -h/--hardware" in err

    def test_missing_token(self, ibm, capsys, monkeypatch):
        monkeypatch.delenv("IBMQ_API_TOKEN")
        code, out, err = self.run(capsys, "-h", "-t", "300", "1", "100")
        assert code == 1
        assert out == ""
        assert "IBMQ_API_TOKEN or QISKIT_IBM_TOKEN" in err

    def test_failed_job(self, ibm, capsys):
        ibm.jobs.append(make_job(["0"], statuses=["QUEUED", "ERROR"], error="backend offline"))
        code, out, err = self.run(capsys, "-h", "-t", "300", "1", "100")
        assert code == 1
        assert out == ""
        assert "ended with status ERROR: backend offline" in err

    def test_interrupt(self, ibm, capsys):
        job = make_job(["0"])
        job.status.side_effect = KeyboardInterrupt
        ibm.jobs.append(job)
        code, out, err = self.run(capsys, "-h", "-t", "300", "1", "100")
        assert code == 1
        assert out == ""
        assert "vqrng: interrupted." in err
        job.cancel.assert_called_once()
