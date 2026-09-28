"""Shot tape, partial recovery, public backends, and Level B verification."""

import copy
import io
import json

import pytest

import vqrng
from vqrng import cli
from vqrng.backends import BackendJobError, BackendRun, BaseBackend
from vqrng.evidence import parse_timestamp, payload_hash
from vqrng.verifier.level_b import verify_level_b
from tests.test_generate import fake_source

pytestmark = pytest.mark.usefixtures("passthrough_extractor")


def rehash(evidence):
    """Re-seal edited evidence so a test isolates one Level B check from the hash check."""
    evidence["pool_hash"] = payload_hash(evidence)
    return evidence


def edited(evidence, mutate, seal=True):
    copy_ = copy.deepcopy(evidence)
    mutate(copy_)
    return verify_level_b(rehash(copy_) if seal else copy_)


class Scripted(BaseBackend):
    """Backend that plays back one scripted outcome per batch."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.budgets = []

    def run(self, circuit, shots, budget):
        self.budgets.append(budget)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def evidence():
    # Range [1, 5] -> 3 bits. 7, 5, 6 are rejected; the pool fills at 4, leaving 3 and 1 unused.
    return vqrng.generate(1, 5, pool_size=3, _source=fake_source([7, 2, 5, 6, 0, 4, 3, 1]))


@pytest.fixture
def partial():
    backend = Scripted(BackendRun(["010", "111"], "fake", job_id="j1"), BackendJobError("job j2 failed", job_id="j2"))
    with pytest.raises(vqrng.GenerationError) as exc:
        vqrng.generate(1, 5, pool_size=3, backend=backend)
    return exc.value.evidence


class TestShotTape:
    def test_unused_tail_of_the_last_batch_is_kept(self, evidence):
        (batch,) = evidence["tape"]
        started, finished = batch.pop("started_at"), batch.pop("finished_at")
        assert batch == {
            "job_id": None, "backend": "fake", "shots": 8,
            "bitstrings": ["111", "010", "101", "110", "000", "100", "011", "001"],
            "isa_sha256": None, "error": None,
        }
        assert parse_timestamp(started) <= parse_timestamp(finished)
        assert evidence["status"] == "completed"
        assert evidence["error"] is None
        assert evidence["version"] == "4"

    def test_pool_hash_commits_to_the_tail(self, evidence):
        evidence["tape"][0]["bitstrings"][-1] = "011"
        result = vqrng.verify(evidence)
        assert not result.is_valid
        assert result.levels["A"].passed
        assert result.levels["B"].errors == [
            "pool_hash does not match the evidence payload; it was changed after generation."
        ]

    def test_filling_on_the_last_allowed_batch_succeeds(self, monkeypatch):
        monkeypatch.setattr("vqrng.core.MAX_BATCHES", 1)
        assert vqrng.generate(0, 1, _source=fake_source([1]))["items"][0]["number"] == 1

    def test_short_bitstrings_from_a_source_are_zero_padded(self):
        evidence = vqrng.generate(0, 7, pool_size=2, _source=lambda c, s, b: (["1", "10"], "fake", 0.0))
        assert [item["bitstring"] for item in evidence["items"]] == ["001", "010"]

    def test_malformed_bitstrings_stop_sampling_and_are_recorded(self):
        backend = Scripted(BackendRun(["2"], "fake", job_id="j1"))
        with pytest.raises(vqrng.GenerationError, match="shot 0 is '2'") as exc:
            vqrng.generate(0, 7, backend=backend)
        evidence = exc.value.evidence
        assert evidence["job_ids"] == ["j1"]
        assert evidence["tape"][0]["bitstrings"] == []
        assert "shot 0" in evidence["tape"][0]["error"]


class TestPartialEvidence:
    def test_values_jobs_and_pending_rejects_survive(self, partial):
        assert partial["status"] == "partial"
        assert partial["error"] == "job j2 failed"
        assert [item["number"] for item in partial["items"]] == [3]
        assert partial["job_ids"] == ["j1", "j2"]
        assert partial["tape"][0]["bitstrings"] == ["010", "111"]
        assert partial["backend"] == "fake"
        assert vqrng.verify(partial).is_valid

    def test_the_original_exception_is_chained(self):
        with pytest.raises(vqrng.GenerationError) as exc:
            vqrng.generate(0, 1, backend=Scripted(KeyError("boom")))
        assert isinstance(exc.value.__cause__, KeyError)
        assert exc.value.evidence["items"] == []
        assert vqrng.verify(exc.value.evidence).is_valid

    def test_budget_below_one_second_starts_no_job(self):
        backend = Scripted(BackendRun(["1"], "fake", 0.2, charged_seconds=1.5))
        with pytest.raises(vqrng.GenerationError, match="exhausted after 0/1"):
            vqrng.generate(0, 0, mode="hardware", runtime_limit=2, backend=backend)
        assert backend.budgets == [2]

    def test_failed_job_charge_counts_against_the_budget(self):
        backend = Scripted(BackendJobError("lost", charged_seconds=7.0))
        with pytest.raises(vqrng.GenerationError) as exc:
            vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend)
        assert exc.value.evidence["charged_seconds"] == 7.0
        assert exc.value.evidence["backend"] is None


class TestPublicBackend:
    def test_backend_instance_in_simulator_mode(self):
        backend = Scripted(BackendRun(["1"], "custom"))
        evidence = vqrng.generate(0, 1, backend=backend)
        assert evidence["backend"] == "custom"
        assert evidence["request"]["backend"] == "Scripted"
        assert backend.budgets == [None]

    def test_backend_instance_in_hardware_mode_gets_the_budget(self):
        backend = Scripted(BackendRun(["1"], "custom", 3.0))
        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=10, backend=backend)
        assert backend.budgets == [10]
        assert evidence["charged_seconds"] == 3.0

    def test_backend_must_be_a_name_or_callable(self):
        with pytest.raises(TypeError, match="backend must be"):
            vqrng.generate(0, 1, backend=42)


class TestLevelB:
    def test_generated_evidence_passes_every_implemented_level(self, evidence):
        result = vqrng.verify(json.dumps(evidence))
        assert result.is_valid and result.errors == []
        assert [(r.level, r.status) for r in result.levels.values()] == [
            ("A", "pass"), ("B", "pass"), ("C", "skipped"),
        ]
        assert result.evidence_status == "completed"

    def test_errors_are_labelled_by_level(self, evidence):
        evidence["items"][0]["number"] = 5
        result = vqrng.verify(evidence)
        assert result.errors[0].startswith("Level A: items[0]")
        assert result.errors[-1].startswith("Level B: pool_hash")

    def test_invalid_json_fails_both_levels_once(self):
        result = vqrng.verify("{")
        assert len(result.errors) == 1
        assert not result.levels["A"].passed and not result.levels["B"].passed

    def test_not_an_object(self):
        assert verify_level_b([]) == (False, ["Evidence must be an object, got list."])

    @pytest.mark.parametrize(("mutate", "message"), [
        (lambda e: e.pop("pool_hash"), "pool_hash is missing."),
        (lambda e: e.update(pool_hash="0" * 64), "pool_hash does not match"),
    ])
    def test_pool_hash(self, evidence, mutate, message):
        ok, errors = edited(evidence, mutate, seal=False)
        assert not ok and errors[0].startswith(message)

    @pytest.mark.parametrize(("mutate", "message"), [
        (lambda e: e["circuit"].pop("sha256"), "circuit.sha256 is missing."),
        (lambda e: e["circuit"].update(sha256="0" * 64), "circuit.sha256 does not match"),
        (lambda e: e["circuit"].update(qasm=e["circuit"]["qasm"] + "\nx q[0];"), "circuit.sha256 does not match"),
        (lambda e: e.pop("circuit"), "circuit.qasm is missing."),
        (lambda e: e.update(version="3"), "version is '3', expected '4'."),
        (lambda e: e["extractor"].update(input_bits=256), "extractor is {"),
        (lambda e: e.pop("extractor"), "extractor is None"),
        (lambda e: e.pop("request"), "request is missing."),
        (lambda e: e["request"].update(min_val="1"), "request does not describe a valid range"),
        (lambda e: e["request"].update(max_val=6), "range is (1, 5), but the request resolves to (1, 6)."),
        (lambda e: e.pop("range"), "range is None"),
        (lambda e: e.update(status="done"), "status is 'done'"),
        (lambda e: e["request"].update(pool_size=0), "request.pool_size must be a positive integer"),
        (lambda e: e["request"].update(pool_size=4), "status is 'completed' but the evidence holds 3 of 4"),
        (lambda e: e.update(status="partial"), "status is 'partial' but the evidence holds all 3"),
        (lambda e: e.pop("tape"), "tape must be a list"),
        (lambda e: e["tape"].append("batch"), "tape[1]: expected an object"),
        (lambda e: e.update(job_ids=["forged"]), "job_ids ['forged'] do not match"),
        (lambda e: e["tape"][0]["bitstrings"].__setitem__(1, "011"),
         "items do not replay from the conditioned shot tape; first difference at candidate 1."),
        (lambda e: e["tape"][0].update(bitstrings=["111", "010"]),
         "items do not replay from the conditioned shot tape; first difference at candidate 2."),
        (lambda e: e["tape"][0]["bitstrings"].append("12"), "tape shot 8 '12' is not a 3-bit binary string."),
    ])
    def test_sealed_edits_are_still_caught(self, evidence, mutate, message):
        ok, errors = edited(evidence, mutate)
        assert not ok
        assert any(error.startswith(message) for error in errors), errors

    def test_items_are_needed_in_a_completed_record(self, evidence):
        ok, errors = edited(evidence, lambda e: e.update(items=None))
        assert not ok
        assert errors == ["status is 'completed' but the evidence holds 0 of 3 values."]

    def test_replay_is_left_to_level_a_when_the_range_is_malformed(self, evidence):
        ok, errors = edited(evidence, lambda e: e.update(n_bits="3"))
        assert ok, errors

    def test_non_object_items_are_skipped_in_replay(self, evidence):
        def mutate(e):
            e["items"].append("bad")
            e["request"]["pool_size"] = 4

        ok, errors = edited(evidence, mutate)
        assert ok, errors

    def test_malformed_rejected_list_is_skipped_in_replay(self, evidence):
        ok, errors = edited(evidence, lambda e: e["items"][1].update(rejected="x"))
        assert not ok
        assert errors == ["items do not replay from the conditioned shot tape; first difference at candidate 2."]

    def test_partial_tail_must_be_all_rejects(self, partial):
        def mutate(e):
            e["tape"][0]["bitstrings"].append("001")
            e["health"]["bits"] += 3

        ok, errors = edited(partial, mutate)
        assert not ok
        assert errors == [
            "conditioned candidate 2 decodes to 1, within range size 5; a partial run would have accepted it."
        ]


class TestCli:
    def run(self, capsys, *argv, stdin=None, monkeypatch=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    def test_verify_reports_each_level(self, evidence, capsys, monkeypatch):
        code, out, _ = self.run(capsys, "verify", stdin=json.dumps(evidence), monkeypatch=monkeypatch)
        assert code == 0
        assert out.splitlines() == [
            "PASS: Level A (reproducible conversion)",
            "PASS: Level B (tamper-evident provenance) via checksum only (self-consistent; not authenticated)",
            "SKIP: Level C (near-real-time CHSH spot-check) was not run; the evidence has no chsh_data",
        ]

    def test_verify_notes_partial_evidence(self, partial, capsys, monkeypatch):
        code, out, _ = self.run(capsys, "verify", stdin=json.dumps(partial), monkeypatch=monkeypatch)
        assert code == 0
        assert "NOTE: partial evidence" in out

    def test_verify_fails_on_a_deleted_hash(self, evidence, capsys, monkeypatch):
        del evidence["pool_hash"]
        code, out, _ = self.run(capsys, "verify", stdin=json.dumps(evidence), monkeypatch=monkeypatch)
        assert code == 1
        assert "FAIL: Level B (tamper-evident provenance)\n  - pool_hash is missing." in out

    def partial_failure(self, monkeypatch, partial):
        def fail(**kwargs):
            raise vqrng.GenerationError("job j2 failed", partial)

        monkeypatch.setattr(cli.vqrng, "generate", fail)

    def test_json_prints_partial_evidence_and_fails(self, partial, capsys, monkeypatch):
        self.partial_failure(monkeypatch, partial)
        code, out, err = self.run(capsys, "-j", "-p", "3", "1", "5")
        assert code == 1
        assert json.loads(out)["status"] == "partial"
        assert "vqrng: error: job j2 failed" in err
        assert "stopped with 1/3 value(s); job ids: j1, j2" in err

    def test_plain_output_prints_no_partial_numbers(self, partial, capsys, monkeypatch):
        partial["job_ids"] = []
        self.partial_failure(monkeypatch, partial)
        code, out, err = self.run(capsys, "-p", "3", "1", "5")
        assert code == 1
        assert out == ""
        assert "job ids: none" in err
        assert "pass -j/--json to keep it" in err
