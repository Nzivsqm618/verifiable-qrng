"""CHSH Bell-test circuits, their evidence and timing, and Level C verification."""

import copy
import io
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

import vqrng
from vqrng import cli
from vqrng.backends import BackendJobError, BackendRun, BaseBackend
from vqrng.evidence import parse_timestamp, payload_hash
from vqrng.quantum import generate_chsh_circuits, tally_counts
from vqrng.verifier.level_c import verify_level_c

QUANTUM = {  # E = +0.713, +0.713, +0.713, -0.713, so S = 2.85
    "A0B0": {"00": 435, "01": 77, "10": 70, "11": 442},
    "A0B1": {"00": 440, "01": 72, "10": 75, "11": 437},
    "A1B0": {"00": 438, "01": 71, "10": 76, "11": 439},
    "A1B1": {"00": 74, "01": 436, "10": 441, "11": 73},
}
T0 = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def window(start=0.0, end=1.0, backend="qpu", **extra):
    return {"job_id": "j1", "backend": backend, "started_at": at(start), "finished_at": at(end), **extra}


_POOL_BATCH = object()


def with_counts(counts, shots=1024, error=None, runs=None, tape=_POOL_BATCH):
    """Evidence holding one pool batch at [0s, 1s] and CHSH runs in the same window."""
    return {
        "backend": "qpu",
        "tape": [window(error=None)] if tape is _POOL_BATCH else tape,
        "chsh_data": {
            "shots": shots,
            "counts": counts,
            "runs": {setting: window() for setting in QUANTUM} if runs is None else runs,
            "error": error,
        },
    }


def uniform(tally):
    return {setting: dict(tally) for setting in QUANTUM}


def shifted(setting, start, end=None, **extra):
    runs = {s: window() for s in QUANTUM}
    runs[setting] = window(start, start + 1 if end is None else end, **extra)
    return runs


class Split(BaseBackend):
    """Pool shots from ``pool``, then one scripted outcome per CHSH setting."""

    def __init__(self, pool, *chsh):
        self.pool = pool
        self.chsh = list(chsh)
        self.calls = []

    def run(self, circuit, shots, budget):
        self.calls.append(circuit.name)
        if circuit.name == "vqrng":
            outcome = self.pool
        else:
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
        assert data["shots"] == 8192 and data["error"] is None
        assert all(sum(tally.values()) == 8192 for tally in data["counts"].values())
        result = vqrng.verify(evidence)
        assert result.is_valid and result.level_c_passed, result.errors
        assert result.chsh_s_value == pytest.approx(2 * math.sqrt(2), abs=0.1)
        assert [(r.level, r.status) for r in result.levels.values()] == [
            ("A", "pass"), ("B", "pass"), ("C", "pass"),
        ]

    def test_chsh_runs_record_their_backend_and_execution_window(self):
        evidence = vqrng.generate(1, 6, chsh=True, chsh_shots=16)
        runs = evidence["chsh_data"]["runs"]
        assert list(runs) == ["A0B0", "A0B1", "A1B0", "A1B1"]
        pool_start = parse_timestamp(evidence["tape"][0]["started_at"])
        for run in runs.values():
            assert run["backend"] == evidence["backend"] == "aer_simulator"
            assert run["job_id"] is None
            start, end = parse_timestamp(run["started_at"]), parse_timestamp(run["finished_at"])
            assert pool_start <= start <= end
            assert (end - pool_start).total_seconds() < 10

    @pytest.mark.parametrize("edit", [
        lambda data: data["counts"]["A1B1"].update({"00": data["counts"]["A1B1"]["00"] + 1}),
        lambda data: data["runs"]["A0B0"].update(started_at=at(0)),
        lambda data: data["runs"]["A0B1"].update(job_id="forged"),
    ])
    def test_pool_hash_covers_chsh_counts_times_and_job_ids(self, edit):
        evidence = vqrng.generate(1, 6, chsh=True, chsh_shots=16)
        edit(evidence["chsh_data"])
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


class TestTimeWindow:
    @pytest.mark.parametrize(("start", "gap"), [(12.0, 11.0), (-12.0, 11.0), (61.0, 60.0)])
    def test_a_run_outside_the_window_fails(self, start, gap):
        ok, errors, s_value = verify_level_c(with_counts(QUANTUM, runs=shifted("A1B1", start)))
        assert not ok and s_value == pytest.approx(2.8516, abs=1e-4)
        assert errors == [f"CHSH setting A1B1 ran {gap:.1f}s from the nearest pool batch, outside the 10s window."]

    @pytest.mark.parametrize("start", [11.0, -11.0, 0.5])
    def test_a_run_within_ten_seconds_or_overlapping_passes(self, start):
        assert verify_level_c(with_counts(QUANTUM, runs=shifted("A0B0", start)))[0]

    def test_the_nearest_pool_batch_counts(self):
        tape = [window(), window(100.0, 101.0, error=None)]
        assert verify_level_c(with_counts(QUANTUM, runs=shifted("A0B1", 105.0), tape=tape))[0]

    def test_zulu_timestamps_are_accepted(self):
        runs = shifted("A0B0", 0)
        runs["A0B0"].update(started_at="2026-09-28T06:00:00Z", finished_at="2026-09-28T06:00:01Z")
        assert verify_level_c(with_counts(QUANTUM, runs=runs))[0]

    @pytest.mark.parametrize("edit", [
        lambda run: run.update(started_at=None),
        lambda run: run.update(finished_at="soon"),
        lambda run: run.update(started_at="2026-09-28T06:00:00"),
        lambda run: run.update(started_at=at(5), finished_at=at(4)),
    ])
    def test_a_run_without_a_valid_window_is_decoupled(self, edit):
        runs = shifted("A1B0", 0)
        edit(runs["A1B0"])
        ok, errors, _ = verify_level_c(with_counts(QUANTUM, runs=runs))
        assert not ok
        assert errors == [
            "chsh_data.runs.A1B0 has no valid started_at/finished_at; the CHSH run is decoupled from the pool."
        ]

    @pytest.mark.parametrize("runs", [{}, None, {"A0B0": "yesterday"}])
    def test_missing_runs_are_decoupled(self, runs):
        data = with_counts(QUANTUM)
        data["chsh_data"]["runs"] = runs
        ok, errors, _ = verify_level_c(data)
        assert not ok and len(errors) == 4
        assert all("is decoupled from the pool" in error for error in errors)

    def test_a_run_on_another_backend_fails(self):
        ok, errors, _ = verify_level_c(with_counts(QUANTUM, runs=shifted("A0B0", 0, backend="ibm_other")))
        assert not ok
        assert errors == ["CHSH setting A0B0 ran on 'ibm_other', but the pool ran on 'qpu'."]

    @pytest.mark.parametrize("tape", [
        [], None, ["batch"], [window(error="job lost")], [{"error": None, "started_at": None}],
    ])
    def test_no_usable_pool_window_fails(self, tape):
        evidence = with_counts(QUANTUM, tape=tape)
        ok, errors, _ = verify_level_c(evidence)
        assert not ok
        assert errors == [
            "no pool batch records valid execution times, so the CHSH runs cannot be tied to the pool."
        ]

    def test_timing_and_violation_errors_are_both_reported(self):
        counts = uniform({"00": 256, "01": 256, "10": 256, "11": 256})
        ok, errors, s_value = verify_level_c(with_counts(counts, runs=shifted("A0B0", 30.0)))
        assert not ok and s_value == 0.0
        assert len(errors) == 2 and errors[1] == "Violation failed: S = 0.0000 <= 2.0"


@pytest.mark.usefixtures("passthrough_extractor")
class TestExecution:
    def test_chsh_shares_the_first_pool_job(self):
        backend = Split(
            BackendRun(["1"] * 16, "qpu", 1.0, job_id="pool"),
            fixed("00", quantum_seconds=0.5, job_id="c0"), fixed("11", job_id="c1"),
            fixed("01", job_id="c2"), ("10 10 10 10".split(), "qpu", 0.25),
        )
        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend, chsh=True, chsh_shots=4)
        assert backend.calls[:5] == ["vqrng", "chsh_A0B0", "chsh_A0B1", "chsh_A1B0", "chsh_A1B1"]
        data = evidence["chsh_data"]
        assert [run["job_id"] for run in data["runs"].values()] == ["c0", "c1", "c2", None]
        assert evidence["job_ids"][0] == "pool"
        assert data["counts"]["A1B0"] == {"00": 0, "01": 0, "10": 4, "11": 0}
        assert evidence["quantum_seconds"] >= 1.75
        assert vqrng.verify(evidence).levels["B"].passed

    def test_later_pool_batches_do_not_rerun_chsh(self):
        backend = Split(BackendRun(["1"] * 8, "fake"), *[fixed("00")] * 4)
        with pytest.raises(vqrng.GenerationError, match="did not converge") as exc:
            vqrng.generate(0, 0, backend=backend, chsh=True, chsh_shots=4)
        assert backend.calls.count("vqrng") == 64
        assert backend.calls.count("chsh_A0B0") == 1
        assert exc.value.evidence["chsh_data"]["error"] is None

    @pytest.mark.parametrize(("job_id", "job_ids"), [("c1", ["c1"]), (None, [])])
    def test_a_failed_job_stops_the_pool_and_the_chsh_test(self, job_id, job_ids):
        backend = Split(
            BackendRun(["1"], "qpu", job_id="pool"), fixed("00", job_id="c0"),
            BackendJobError("lost", job_id=job_id, charged_seconds=3.0),
        )
        with pytest.raises(vqrng.GenerationError, match="lost") as exc:
            vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend, chsh=True, chsh_shots=4)
        evidence = exc.value.evidence
        assert evidence["status"] == "partial"
        assert evidence["job_ids"] == job_ids
        assert evidence["charged_seconds"] == 3.0
        assert evidence["chsh_data"]["error"] == "not run: lost"

    def test_malformed_chsh_shots_are_an_error(self):
        backend = Split(BackendRun(["1"] * 16, "fake"), fixed("2"), *[fixed("00")] * 3)
        with pytest.raises(vqrng.GenerationError, match="CHSH test did not finish: CHSH setting A0B0: shot 0") as exc:
            vqrng.generate(0, 1, backend=backend, chsh=True, chsh_shots=4)
        evidence = exc.value.evidence
        assert evidence["status"] == "completed"
        assert "A0B0" not in evidence["chsh_data"]["counts"]
        result = vqrng.verify(evidence)
        assert result.levels["A"].passed and result.levels["B"].passed
        assert result.levels["C"].errors[0].startswith("the CHSH run did not finish: CHSH setting A0B0")

    def test_the_first_malformed_setting_is_reported(self):
        backend = Split(BackendRun(["1"] * 16, "fake"), fixed("2"), fixed("3"), *[fixed("00")] * 2)
        with pytest.raises(vqrng.GenerationError, match="CHSH setting A0B0") as exc:
            vqrng.generate(0, 1, backend=backend, chsh=True, chsh_shots=4)
        assert "A0B1" not in exc.value.evidence["chsh_data"]["error"]

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
            "PASS: Level B (tamper-evident provenance) via checksum only (self-consistent; not authenticated)",
        ]
        assert lines[2].startswith("Level C (near-real-time CHSH spot-check): PASSED (S = ")
        assert 2.0 < float(lines[2].split("S = ")[1].rstrip(")")) <= 3.0

    def seal(self, edit):
        evidence = copy.deepcopy(vqrng.generate(1, 6, chsh=True, chsh_shots=1024))
        edit(evidence["chsh_data"])
        evidence["pool_hash"] = payload_hash(evidence)
        return json.dumps(evidence)

    def test_classical_counts_fail_verify(self, capsys, monkeypatch):
        stdin = self.seal(lambda data: data.update(counts=uniform({"00": 256, "01": 256, "10": 256, "11": 256})))
        code, out, _ = run_cli(capsys, "verify", stdin=stdin, monkeypatch=monkeypatch)
        assert code == 1
        assert "Level C (near-real-time CHSH spot-check): FAILED (S = 0.00)\n" \
               "  - Violation failed: S = 0.0000 <= 2.0" in out

    def test_a_late_chsh_run_fails_verify(self, capsys, monkeypatch):
        def delay(data):
            start = parse_timestamp(data["runs"]["A1B1"]["finished_at"]) + timedelta(seconds=30)
            data["runs"]["A1B1"].update(started_at=start.isoformat(), finished_at=start.isoformat())

        code, out, _ = run_cli(capsys, "verify", stdin=self.seal(delay), monkeypatch=monkeypatch)
        assert code == 1
        assert "  - CHSH setting A1B1 ran 3" in out and "outside the 10s window." in out

    def test_unusable_counts_fail_without_an_s_value(self, capsys, monkeypatch):
        code, out, _ = run_cli(capsys, "verify", stdin=self.seal(lambda d: d.update(counts={})), monkeypatch=monkeypatch)
        assert code == 1
        assert "Level C (near-real-time CHSH spot-check): FAILED\n  - chsh_data.counts.A0B0" in out
