"""Continuous health tests (SP 800-90B RCT and APT) on the raw tape, before conditioning."""

import copy
import io
import json

import pytest

import vqrng
from vqrng import cli, health
from vqrng.evidence import payload_hash
from vqrng.health import APT_WINDOW, HealthMonitor, apt_cutoff, rct_cutoff
from vqrng.verifier.level_b import verify_level_b


def alternating(circuit, shots, budget):
    """Perfectly predictable, yet passes both health tests: runs of one, half ones."""
    width = circuit.num_clbits
    stream = "01" * (width * shots)
    return [stream[i * width:(i + 1) * width] for i in range(shots)], "fake", 0.0


def stuck(bit):
    def source(circuit, shots, budget):
        return [bit * circuit.num_clbits] * shots, "fake", 0.0

    return source


def biased(circuit, shots, budget):
    """Mostly ones, but never more than 7 in a row: passes the RCT, fails the APT."""
    width = circuit.num_clbits
    stream = ("1" * 7 + "0") * (width * shots)
    return [stream[i * width:(i + 1) * width] for i in range(shots)], "fake", 0.0


def run(monitor_bits):
    monitor = HealthMonitor()
    monitor.feed(monitor_bits)
    return monitor


class TestCutoffs:
    def test_assumed_min_entropy_gives_the_documented_cutoffs(self):
        assert (health.H_MIN, health.ALPHA_EXPONENT, APT_WINDOW) == (0.5, 20, 1024)
        assert rct_cutoff(0.5) == 41
        assert apt_cutoff(0.5) == 793

    def test_full_entropy_matches_the_sp_800_90b_binary_values(self):
        assert rct_cutoff(1.0) == 21
        assert apt_cutoff(1.0) == 589

    def test_a_near_zero_rate_cuts_off_at_one(self):
        assert apt_cutoff(40.0) == 1


class TestMonitor:
    def test_repetition_count_fails_at_the_cutoff(self):
        assert run("0" + "1" * 40).failure is None
        failure = run("0" + "1" * 41).failure
        assert failure == "repetition count test failed: 41 identical raw bits in a row, cutoff 41 (assumed H_min = 0.5)."

    def test_runs_continue_across_feeds(self):
        monitor = HealthMonitor()
        for _ in range(41):
            monitor.feed("1")
        assert monitor.longest_run == 41 and monitor.failure is not None

    def test_adaptive_proportion_counts_the_first_bit_of_each_window(self):
        ok = run(("1" * 3 + "0") * 256)
        assert (ok.windows, ok.max_count, ok.failure) == (1, 768, None)
        # 896 ones, but the window starts with 0, so the test counts the 128 zeros.
        assert run(("0" + "1" * 7) * 128).max_count == 128
        bad = run(("1" * 7 + "0") * 128)
        assert bad.max_count == 896
        assert bad.failure == (
            "adaptive proportion test failed: 896 of 1024 raw bits in a window equal its first bit, "
            "cutoff 793 (assumed H_min = 0.5)."
        )

    def test_windows_span_feeds_and_a_short_tail_is_ignored(self):
        monitor = HealthMonitor()
        monitor.feed("01" * 300)
        monitor.feed("01" * 300)
        assert (monitor.bits, monitor.windows, monitor.max_count) == (1200, 1, 512)

    def test_summary_before_a_full_window(self):
        assert run("0110").summary() == {
            "h_min": 0.5, "assumed": True, "alpha_exponent": 20, "bits": 4,
            "rct": {"cutoff": 41, "longest_run": 2, "passed": True},
            "apt": "not_enough_bits",
        }

    def test_summary_after_full_windows(self):
        assert run("01" * 1100).summary()["apt"] == {
            "cutoff": 793, "window": 1024, "windows": 2, "max_count": 512, "passed": True,
        }

    def test_empty_stream_passes(self):
        assert run("").failure is None


class TestGeneration:
    def test_a_stuck_source_is_stopped_before_conditioning(self):
        with pytest.raises(vqrng.GenerationError, match="repetition count test failed") as exc:
            vqrng.generate(1, 100, pool_size=5, _source=stuck("0"))
        evidence = exc.value.evidence
        assert isinstance(exc.value.__cause__, vqrng.EntropyHealthError)
        assert evidence["status"] == "partial" and evidence["items"] == []
        assert evidence["tape"][-1]["error"].startswith("repetition count test failed")
        assert evidence["tape"][-1]["bitstrings"]
        assert evidence["health"]["rct"]["passed"] is False
        result = vqrng.verify(evidence)
        assert result.is_valid, result.errors
        assert result.evidence_status == "partial"

    def test_a_biased_source_fails_the_proportion_test(self):
        with pytest.raises(vqrng.GenerationError, match="adaptive proportion test failed") as exc:
            vqrng.generate(1, 100, pool_size=100, _source=biased)
        evidence = exc.value.evidence
        assert evidence["health"]["rct"]["passed"] is True
        assert evidence["health"]["apt"]["passed"] is False
        assert vqrng.verify(evidence).is_valid

    def test_a_predictable_source_still_passes(self):
        # The tests catch faults. They do not measure entropy.
        first = vqrng.generate(1, 100, pool_size=100, _source=alternating)
        second = vqrng.generate(1, 100, pool_size=100, _source=alternating)
        assert first["health"]["bits"] >= 2 * APT_WINDOW
        assert first["items"] == second["items"]
        assert first["health"]["rct"] == {"cutoff": 41, "longest_run": 1, "passed": True}
        assert first["health"]["apt"]["passed"] is True
        assert vqrng.verify(first).is_valid

    def test_a_short_run_records_that_the_proportion_test_did_not_run(self):
        evidence = vqrng.generate(1, 6, _source=alternating)
        assert evidence["health"]["bits"] < APT_WINDOW
        assert evidence["health"]["apt"] == "not_enough_bits"

    def test_aer_passes(self):
        evidence = vqrng.generate(1, 100, pool_size=10)
        assert evidence["health"]["rct"]["passed"] is True
        assert vqrng.verify(evidence).is_valid

    def test_cli_exits_1_and_keeps_partial_json(self, capsys, monkeypatch):
        real = vqrng.generate
        monkeypatch.setattr(vqrng, "generate", lambda **kw: real(**kw, _source=stuck("1")))
        code = cli.main(["-j", "-p", "3", "1", "100"])
        out, err = capsys.readouterr()
        assert code == 1
        assert "vqrng: error: repetition count test failed" in err
        assert json.loads(out)["status"] == "partial"


class TestLevelB:
    @pytest.fixture
    def evidence(self):
        return vqrng.generate(1, 100, pool_size=5, _source=alternating)

    def reseal(self, evidence, mutate):
        edited = copy.deepcopy(evidence)
        mutate(edited)
        edited["pool_hash"] = payload_hash(edited)
        return verify_level_b(edited)

    def test_a_loosened_record_is_caught(self, evidence):
        ok, errors = self.reseal(evidence, lambda e: e["health"].update(h_min=1.0))
        assert not ok
        assert errors[0].startswith("health is {'h_min': 1.0")
        assert "but the raw tape gives {'h_min': 0.5" in errors[0]

    def test_a_missing_record_is_caught(self, evidence):
        ok, errors = self.reseal(evidence, lambda e: e.pop("health"))
        assert errors[0].startswith("health is None, but the raw tape gives")

    def test_a_tape_edited_into_a_stuck_run_is_caught(self, evidence):
        def mutate(e):
            e["tape"][0]["bitstrings"][:8] = ["0000000"] * 8

        ok, errors = self.reseal(evidence, mutate)
        assert not ok
        assert any(error.startswith("health is {") for error in errors)

    def test_a_failing_tape_cannot_be_completed(self, evidence):
        def mutate(e):
            e["tape"][0]["bitstrings"][:8] = ["0000000"] * 8
            monitor = HealthMonitor()
            monitor.feed("".join(b for batch in e["tape"] for b in batch["bitstrings"]))
            e["health"] = monitor.summary()

        ok, errors = self.reseal(evidence, mutate)
        assert not ok
        assert "the raw tape fails a health test, but status is 'completed': repetition count test failed" in errors[0]

    def test_the_failed_batch_is_not_replayed_as_unused_candidates(self):
        with pytest.raises(vqrng.GenerationError) as exc:
            vqrng.generate(1, 100, pool_size=5, _source=stuck("0"))
        assert verify_level_b(exc.value.evidence) == (True, [])

    def test_verify_cli_reports_a_health_edit(self, evidence, capsys, monkeypatch):
        evidence["health"]["assumed"] = False
        evidence["pool_hash"] = payload_hash(evidence)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(evidence)))
        code = cli.main(["verify"])
        out = capsys.readouterr().out
        assert code == 1
        assert "FAIL: Level B" in out and "  - health is {" in out
