"""CHSH Bell-test circuits, their evidence, and Level C verification."""

import copy
import io
import json
import math

import pytest

import vqrng
from vqrng import cli
from vqrng.backends import BackendJobError, BackendRun, BaseBackend
from vqrng.evidence import payload_hash
from vqrng.quantum import generate_chsh_circuits, tally_counts
from vqrng.verifier.level_c import verify_level_c

QUANTUM = {  # E = +0.713, +0.713, +0.713, -0.713, so S = 2.85
    "A0B0": {"00": 435, "01": 77, "10": 70, "11": 442},
    "A0B1": {"00": 440, "01": 72, "10": 75, "11": 437},
    "A1B0": {"00": 438, "01": 71, "10": 76, "11": 439},
    "A1B1": {"00": 74, "01": 436, "10": 441, "11": 73},
}


def with_counts(counts, shots=1024, error=None):
    return {"chsh_data": {"shots": shots, "counts": counts, "job_ids": [], "error": error}}


def uniform(tally):
    return {setting: dict(tally) for setting in QUANTUM}


class Split(BaseBackend):
    """Pool shots from ``pool``, then one scripted outcome per CHSH setting."""

    def __init__(self, pool, *chsh):
        self.pool = pool
        self.chsh = list(chsh)
        self.settings = []

    def run(self, circuit, shots, budget):
        if circuit.name == "vqrng":
            outcome = self.pool
        else:
            self.settings.append(circuit.name)
            outcome = self.chsh.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fixed(bits, **kwargs):
    return BackendRun([bits] * 4, "fake", **kwargs)


def run_cli(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


class TestCircuits:
    def test_four_distinct_bell_circuits(self):
        circuits = generate_chsh_circuits()
        assert list(circuits) == ["A0B0", "A0B1", "A1B0", "A1B1"]
        assert len({str(qc.data) for qc in circuits.values()}) == 4
        for qc in circuits.values():
            assert (qc.num_qubits, qc.num_clbits) == (2, 2)
            assert [i.operation.name for i in qc.data[:2]] == ["h", "cx"]
            assert [qc.find_bit(q).index for q in qc.data[1].qubits] == [0, 1]
            assert qc.count_ops()["measure"] == 2

    @pytest.mark.parametrize(("setting", "alice_h", "angle"), [
        ("A0B0", False, -math.pi / 4),
        ("A0B1", False, math.pi / 4),
        ("A1B0", True, -math.pi / 4),
        ("A1B1", True, math.pi / 4),
    ])
    def test_measurement_bases(self, setting, alice_h, angle):
        qc = generate_chsh_circuits()[setting]
        assert qc.count_ops()["h"] == (2 if alice_h else 1)
        (ry,) = [i for i in qc.data if i.operation.name == "ry"]
        assert qc.find_bit(ry.qubits[0]).index == 1
        assert ry.operation.params[0] == pytest.approx(angle)

    def test_tally_is_alice_first(self):
        assert tally_counts(["01", "01", "11"]) == {"00": 0, "01": 0, "10": 2, "11": 1}


class TestAer:
    def test_bell_state_violates_the_classical_bound(self):
        evidence = vqrng.generate(1, 100, pool_size=3, chsh=True, chsh_shots=8192)
        data = evidence["chsh_data"]
        assert data["shots"] == 8192 and data["error"] is None and data["job_ids"] == []
        assert all(sum(tally.values()) == 8192 for tally in data["counts"].values())
        result = vqrng.verify(evidence)
        assert result.is_valid and result.level_c_passed
        assert result.chsh_s_value == pytest.approx(2 * math.sqrt(2), abs=0.1)
        assert [(r.level, r.status) for r in result.levels.values()] == [
            ("A", "pass"), ("B", "pass"), ("C", "pass"),
        ]

    def test_pool_hash_covers_the_chsh_counts(self):
        evidence = vqrng.generate(1, 6, chsh=True, chsh_shots=16)
        evidence["chsh_data"]["counts"]["A1B1"]["00"] += 1
        evidence["chsh_data"]["counts"]["A1B1"]["01"] -= 1
        result = vqrng.verify(evidence)
        assert result.levels["B"].errors == [
            "pool_hash does not match the evidence payload; it was changed after generation."
        ]

    def test_without_chsh_level_c_is_skipped(self):
        evidence = vqrng.generate(1, 6)
        assert "chsh_data" not in evidence
        result = vqrng.verify(evidence)
        assert result.is_valid and not result.level_c_passed and result.chsh_s_value is None
        assert result.levels["C"].status == "skipped"


class TestLevelC:
    def test_quantum_counts_pass(self):
        ok, errors, s_value = verify_level_c(with_counts(QUANTUM))
        assert (ok, errors) == (True, [])
        assert s_value == pytest.approx(2.8516, abs=1e-4)

    @pytest.mark.parametrize(("counts", "s_value"), [
        (uniform({"00": 512, "01": 0, "10": 0, "11": 512}), 2.0),  # deterministic local strategy
        (uniform({"00": 256, "01": 256, "10": 256, "11": 256}), 0.0),  # independent coins
    ])
    def test_classical_counts_fail(self, counts, s_value):
        ok, errors, s = verify_level_c(with_counts(counts))
        assert not ok and s == pytest.approx(s_value)
        assert errors == [f"Violation failed: S = {s_value:.4f} <= 2.0"]

    @pytest.mark.parametrize(("evidence", "message"), [
        ([], "Evidence must be an object, got list."),
        ({}, "chsh_data is missing."),
        (with_counts(QUANTUM, error="job lost"), "the CHSH run did not finish: job lost"),
        (with_counts(QUANTUM, shots=0), "chsh_data.shots must be a positive integer, got 0."),
        (with_counts(QUANTUM, shots=True), "chsh_data.shots must be a positive integer, got True."),
        (with_counts(None), "chsh_data.counts.A0B0 must map exactly 00, 01, 10, 11 to counts."),
        (with_counts({**QUANTUM, "A1B0": {"00": 1024}}), "chsh_data.counts.A1B0 must map exactly"),
        (with_counts({**QUANTUM, "A0B1": {"00": 1025, "01": -1, "10": 0, "11": 0}}),
         "chsh_data.counts.A0B1 counts must be non-negative integers."),
        (with_counts({**QUANTUM, "A0B1": {"00": 1024.0, "01": 0, "10": 0, "11": 0}}),
         "chsh_data.counts.A0B1 counts must be non-negative integers."),
        (with_counts(QUANTUM, shots=1000), "chsh_data.counts.A0B0 holds 1024 shots, expected 1000."),
    ])
    def test_unusable_data(self, evidence, message):
        ok, errors, s_value = verify_level_c(evidence)
        assert not ok and s_value is None
        assert errors[0].startswith(message), errors

    def test_non_object_evidence_skips_level_c(self):
        result = vqrng.verify([])
        assert result.levels["C"].status == "skipped" and result.chsh_s_value is None


class TestExecution:
    def test_every_setting_runs_on_the_same_backend(self):
        backend = Split(
            BackendRun(["1"], "qpu", 1.0, job_id="pool"),
            fixed("00", quantum_seconds=0.5, job_id="c0"), fixed("11", job_id="c1"),
            fixed("01", job_id="c2"), ("10 10 10 10".split(), "qpu", 0.25),
        )
        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend, chsh=True, chsh_shots=4)
        assert backend.settings == ["chsh_A0B0", "chsh_A0B1", "chsh_A1B0", "chsh_A1B1"]
        data = evidence["chsh_data"]
        assert data["job_ids"] == ["c0", "c1", "c2"]
        assert evidence["job_ids"] == ["pool"]
        assert data["counts"]["A1B0"] == {"00": 0, "01": 0, "10": 4, "11": 0}
        assert evidence["quantum_seconds"] == 1.75
        assert vqrng.verify(evidence).levels["B"].passed

    def test_budget_exhausted_before_a_setting(self):
        backend = Split(BackendRun(["1"], "qpu", 1.0, job_id="pool"), fixed("00", quantum_seconds=8.5, job_id="c0"))
        with pytest.raises(vqrng.GenerationError, match="CHSH test did not finish") as exc:
            vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend, chsh=True, chsh_shots=4)
        evidence = exc.value.evidence
        assert evidence["status"] == "completed"
        assert evidence["chsh_data"]["error"] == "QPU runtime budget of 10s exhausted before CHSH setting A0B1."
        result = vqrng.verify(evidence)
        assert result.levels["A"].passed and result.levels["B"].passed
        assert result.levels["C"].errors == [
            "the CHSH run did not finish: QPU runtime budget of 10s exhausted before CHSH setting A0B1."
        ]

    @pytest.mark.parametrize(("job_id", "job_ids"), [("c1", ["c0", "c1"]), (None, ["c0"])])
    def test_failed_job_is_charged_and_recorded(self, job_id, job_ids):
        backend = Split(
            BackendRun(["1"], "qpu", job_id="pool"), fixed("00", job_id="c0"),
            BackendJobError("lost", job_id=job_id, charged_seconds=3.0),
        )
        with pytest.raises(vqrng.GenerationError, match="CHSH test did not finish: lost") as exc:
            vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend, chsh=True, chsh_shots=4)
        assert exc.value.evidence["chsh_data"]["job_ids"] == job_ids
        assert exc.value.evidence["charged_seconds"] == 3.0

    def test_malformed_chsh_shots_are_an_error(self):
        backend = Split(BackendRun(["1"], "fake"), fixed("2"))
        with pytest.raises(vqrng.GenerationError, match="shot 0 is '2'"):
            vqrng.generate(0, 1, backend=backend, chsh=True, chsh_shots=4)

    def test_a_stopped_pool_skips_the_chsh_test(self):
        backend = Split(BackendJobError("pool lost"))
        with pytest.raises(vqrng.GenerationError, match="pool lost") as exc:
            vqrng.generate(0, 1, backend=backend, chsh=True)
        assert "chsh_data" not in exc.value.evidence
        assert backend.settings == []

    def test_chsh_shots_must_be_positive(self):
        with pytest.raises(ValueError, match="chsh_shots must be >= 1"):
            vqrng.generate(0, 1, chsh=True, chsh_shots=0)


class TestCli:
    def test_chsh_flag_reaches_generate(self, monkeypatch, capsys):
        seen = {}

        def fake_generate(**kwargs):
            seen.update(kwargs)
            return {"items": [{"number": 1, "formatted": "1"}]}

        monkeypatch.setattr(cli.vqrng, "generate", fake_generate)
        assert run_cli(capsys, "--chsh", "1", "6")[0] == 0
        assert seen["chsh"] is True
        run_cli(capsys, "1", "6")
        assert seen["chsh"] is False

    def test_chsh_json_pipes_into_verify(self, capsys, monkeypatch):
        code, out, _ = run_cli(capsys, "--chsh", "-j", "1", "100")
        assert code == 0
        assert "chsh_data" in json.loads(out)
        code, report, _ = run_cli(capsys, "verify", stdin=out, monkeypatch=monkeypatch)
        assert code == 0
        lines = report.splitlines()
        assert lines[:2] == [
            "PASS: Level A (reproducible conversion)",
            "PASS: Level B (tamper-evident provenance)",
        ]
        assert lines[2].startswith("Level C (Physical CHSH Non-locality): PASSED (S = ")
        assert 2.0 < float(lines[2].split("S = ")[1].rstrip(")")) <= 3.0

    def seal(self, counts):
        evidence = copy.deepcopy(vqrng.generate(1, 6, chsh=True, chsh_shots=1024))
        evidence["chsh_data"]["counts"] = counts
        evidence["pool_hash"] = payload_hash(evidence)
        return json.dumps(evidence)

    def test_classical_counts_fail_verify(self, capsys, monkeypatch):
        stdin = self.seal(uniform({"00": 256, "01": 256, "10": 256, "11": 256}))
        code, out, _ = run_cli(capsys, "verify", stdin=stdin, monkeypatch=monkeypatch)
        assert code == 1
        assert "Level C (Physical CHSH Non-locality): FAILED (S = 0.00)\n" \
               "  - Violation failed: S = 0.0000 <= 2.0" in out

    def test_unusable_counts_fail_without_an_s_value(self, capsys, monkeypatch):
        code, out, _ = run_cli(capsys, "verify", stdin=self.seal({}), monkeypatch=monkeypatch)
        assert code == 1
        assert "Level C (Physical CHSH Non-locality): FAILED\n  - chsh_data.counts.A0B0" in out
