"""Budget overrun flagging and partial recovery when a QPU job is cancelled."""

import pytest
from qiskit_ibm_runtime.exceptions import IBMBackendValueError, RuntimeJobFailureError

import vqrng
from vqrng.backends import BackendRun


def overrun(circuit, shots, budget):
    assert budget == 2
    return BackendRun(["0"], "ibm_fez", quantum_seconds=3.0, job_id="job-over")


class TestBudgetExceeded:
    def test_a_finished_pool_is_kept_and_flagged(self, capsys):
        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=2, backend=overrun)
        assert evidence["status"] == "completed"
        assert evidence["budget_exceeded"] is True
        assert evidence["charged_seconds"] == 3.0
        assert evidence["items"][0]["number"] == 0
        assert "exceeds the 2s budget" in capsys.readouterr().err
        assert vqrng.verify(evidence).is_valid

    def test_spending_the_budget_exactly_is_not_an_overrun(self, capsys):
        def source(circuit, shots, budget):
            return BackendRun(["0"], "ibm_fez", quantum_seconds=2.0, job_id="job-exact")

        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=2, backend=source)
        assert evidence["budget_exceeded"] is False
        assert "exceeds" not in capsys.readouterr().err
        assert vqrng.verify(evidence).is_valid

    def test_an_overrun_that_stops_the_pool_is_partial_and_flagged(self, capsys):
        def source(circuit, shots, budget):
            return BackendRun(["1"], "ibm_fez", quantum_seconds=3.0, job_id="job-over")

        with pytest.raises(vqrng.GenerationError, match="exhausted after 0/1") as exc:
            vqrng.generate(0, 0, mode="hardware", runtime_limit=2, backend=source)
        evidence = exc.value.evidence
        assert evidence["status"] == "partial"
        assert evidence["budget_exceeded"] is True
        assert evidence["tape"][0]["bitstrings"] == ["1"]
        assert "budget_exceeded" in capsys.readouterr().err
        assert vqrng.verify(evidence).is_valid


class TestCancellationRecovery:
    def test_a_runtime_timeout_keeps_the_values_already_accepted(self):
        calls = {"n": 0}

        def source(circuit, shots, budget):
            calls["n"] += 1
            if calls["n"] == 1:
                return BackendRun(["010", "111"], "ibm_fez", quantum_seconds=1.0, job_id="job-1")
            raise RuntimeJobFailureError("cancelled: exceeded the maximum execution time")

        with pytest.raises(vqrng.GenerationError, match="maximum execution time") as exc:
            vqrng.generate(1, 5, pool_size=3, mode="hardware", runtime_limit=10, backend=source)
        evidence = exc.value.evidence
        assert [item["number"] for item in evidence["items"]] == [3]
        assert evidence["job_ids"] == ["job-1"]
        assert evidence["tape"][0]["bitstrings"] == ["010", "111"]
        assert evidence["tape"][1]["bitstrings"] == []
        assert "maximum execution time" in evidence["tape"][1]["error"]
        assert evidence["budget_exceeded"] is False
        assert vqrng.verify(evidence).is_valid

    def test_a_backend_value_error_also_stops_with_partial_evidence(self):
        def source(circuit, shots, budget):
            raise IBMBackendValueError("backend rejected the execution time")

        with pytest.raises(vqrng.GenerationError, match="rejected the execution time") as exc:
            vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=source)
        evidence = exc.value.evidence
        assert evidence["items"] == []
        assert evidence["status"] == "partial"
        assert vqrng.verify(evidence).is_valid
