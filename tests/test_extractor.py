"""HMAC-SHA256 conditioning, with the real extractor end to end."""

import copy
import hashlib
import hmac

import pytest

import vqrng
from vqrng.backends import BackendRun, BaseBackend
from vqrng.evidence import payload_hash
from vqrng.extractor import (
    EXTRACTOR,
    EXTRACTOR_INPUT_BITS,
    EXTRACTOR_OUTPUT_BITS,
    EXTRACTOR_SALT,
    Conditioner,
    condition_block,
    digest_bits,
    extract_entropy,
)
from vqrng.verifier.level_b import verify_level_b


def counter_bits(count, width=EXTRACTOR_INPUT_BITS):
    """Distinct, highly structured raw blocks: the binary expansion of 0, 1, 2, ..."""
    return [format(i, f"0{width}b") for i in range(count)]


def constant(bit):
    def source(circuit, shots, budget):
        return [bit * circuit.num_clbits] * shots, "fake", 0.0

    return source


class TestExtractEntropy:
    def test_is_hmac_sha256_of_the_ascii_bits_keyed_by_the_salt(self):
        raw = "0110" * 128
        assert extract_entropy(raw) == hmac.new(b"vqrng-v1-extractor", raw.encode(), hashlib.sha256).digest()
        assert len(extract_entropy(raw)) == 32
        assert EXTRACTOR_SALT == b"vqrng-v1-extractor"

    def test_is_deterministic_and_depends_on_input_and_salt(self):
        raw = "1" * 512
        assert extract_entropy(raw) == extract_entropy(raw)
        assert extract_entropy(raw) != extract_entropy("1" * 511 + "0")
        assert extract_entropy(raw) != extract_entropy(raw, salt=b"other")

    @pytest.mark.parametrize("raw", ["", "012", 5, None, "1 0"])
    def test_rejects_anything_but_a_bitstring(self, raw):
        with pytest.raises(ValueError, match="raw_bitstring must be"):
            extract_entropy(raw)

    def test_output_bits_are_balanced_and_independent_of_input_structure(self):
        # 4096 counter blocks differ in only their low 12 bits. Each output bit
        # should still be set in close to half of them (sigma = 32 over 4096).
        outputs = [digest_bits(extract_entropy(raw)) for raw in counter_bits(4096)]
        ones_per_position = [sum(out[i] == "1" for out in outputs) for i in range(EXTRACTOR_OUTPUT_BITS)]
        assert all(abs(ones - 2048) < 6 * 32 for ones in ones_per_position)
        assert abs(sum(ones_per_position) / (4096 * 256) - 0.5) < 0.002

    def test_byte_values_are_uniform(self):
        counts = [0] * 256
        for raw in counter_bits(2048):
            for byte in extract_entropy(raw):
                counts[byte] += 1
        expected = 2048 * 32 / 256
        chi_square = sum((c - expected) ** 2 / expected for c in counts)
        assert chi_square < 350  # 255 degrees of freedom; p ~ 1e-4 at 350


class TestConditioner:
    def test_one_block_yields_its_digest_bits(self):
        raw = "01" * 256
        (candidate,) = Conditioner(EXTRACTOR_OUTPUT_BITS).feed([raw])
        assert candidate == condition_block(raw) == digest_bits(extract_entropy(raw))

    def test_partial_blocks_and_candidates_carry_over(self):
        conditioner = Conditioner(100)
        assert conditioner.feed(["1" * 300]) == []
        assert conditioner.raw_pending == 300
        first = conditioner.feed(["0" * 212])
        stream = condition_block("1" * 300 + "0" * 212)
        assert first == [stream[:100], stream[100:200]]
        assert (conditioner.raw_pending, conditioner.conditioned_pending) == (0, 56)
        second = conditioner.feed(["1" * 512])
        assert second[0] == stream[200:] + condition_block("1" * 512)[:44]

    def test_shots_are_one_continuous_stream(self):
        shots = ["101"] * 400
        joined = "".join(shots)
        assert Conditioner(8).feed(shots) == Conditioner(8).feed([joined[:700], joined[700:]])

    def test_evidence_records_the_extractor(self):
        evidence = vqrng.generate(1, 6, _source=constant("1"))
        assert evidence["extractor"] == EXTRACTOR == {
            "name": "hmac-sha256", "salt": b"vqrng-v1-extractor".hex(), "input_bits": 512, "output_bits": 256,
        }


class TestEndToEnd:
    def test_items_come_from_the_conditioned_stream_not_the_raw_bits(self):
        evidence = vqrng.generate(1, 100, pool_size=5, _source=constant("0"))
        raw = [bits for batch in evidence["tape"] for bits in batch["bitstrings"]]
        assert set(raw) == {"0000000"}
        assert len(raw) * 7 >= EXTRACTOR_INPUT_BITS
        assert len({item["number"] for item in evidence["items"]}) > 1
        assert vqrng.verify(evidence).is_valid

    def test_conditioning_cannot_create_entropy(self):
        # A constant source still yields the same "random-looking" pool every time.
        first = vqrng.generate(1, 100, pool_size=5, _source=constant("0"))
        second = vqrng.generate(1, 100, pool_size=5, _source=constant("0"))
        assert [i["number"] for i in first["items"]] == [i["number"] for i in second["items"]]

    def test_aer_pool_verifies_with_real_conditioning(self):
        evidence = vqrng.generate(digits=6, pad=True, pool_size=20)
        result = vqrng.verify(evidence)
        assert result.is_valid, result.errors
        assert all(len(item["formatted"]) == 6 for item in evidence["items"])
        assert sum(batch["shots"] for batch in evidence["tape"]) * 20 >= EXTRACTOR_INPUT_BITS

    def test_one_flipped_raw_bit_breaks_the_replay(self):
        evidence = vqrng.generate(1, 100, pool_size=3, _source=constant("0"))
        edited = copy.deepcopy(evidence)
        edited["tape"][0]["bitstrings"][0] = "0000001"
        edited["pool_hash"] = payload_hash(edited)
        ok, errors = verify_level_b(edited)
        assert not ok
        assert errors == ["items do not replay from the conditioned shot tape; first difference at candidate 0."]

    def test_items_consistent_with_level_a_but_not_the_tape_fail_level_b(self):
        forged = vqrng.generate(0, 1, pool_size=2, _source=constant("1"))
        first = forged["items"][0]
        first["bitstring"] = "0" if first["bitstring"] == "1" else "1"
        first["number"] = int(first["bitstring"])
        first["formatted"] = first["bitstring"]
        forged["pool_hash"] = payload_hash(forged)
        result = vqrng.verify(forged)
        assert result.levels["A"].passed
        assert result.levels["B"].errors == [
            "items do not replay from the conditioned shot tape; first difference at candidate 0."
        ]

    def test_zero_n_bits_leaves_the_replay_to_level_a(self):
        evidence = vqrng.generate(0, 1, _source=constant("1"))
        evidence["n_bits"] = 0
        evidence["pool_hash"] = payload_hash(evidence)
        assert verify_level_b(evidence) == (True, [])


class TestBackendRunMany:
    def test_results_must_match_the_circuits(self):
        class Short(BaseBackend):
            def run(self, circuit, shots, budget):
                raise AssertionError("run_many is overridden")

            def run_many(self, circuits, budget):
                return []

        with pytest.raises(vqrng.GenerationError, match="returned 0 results for 1 circuits"):
            vqrng.generate(0, 1, backend=Short())

    def test_run_many_may_return_tuples_without_times(self):
        class Tuples(BaseBackend):
            def run(self, circuit, shots, budget):
                raise AssertionError("run_many is overridden")

            def run_many(self, circuits, budget):
                return [(["1"] * shots, "tuple-qpu", 0.5) for _, shots in circuits]

        evidence = vqrng.generate(0, 1, backend=Tuples())
        batch = evidence["tape"][0]
        assert evidence["backend"] == "tuple-qpu"
        assert batch["started_at"] <= batch["finished_at"]
        assert vqrng.verify(evidence).is_valid

    def test_reported_times_are_kept(self):
        class Timed(BaseBackend):
            def run(self, circuit, shots, budget):
                return BackendRun(["0"] * shots, "timed", started_at="2026-09-28T06:00:00+00:00",
                                  finished_at="2026-09-28T06:00:02+00:00")

        batch = vqrng.generate(0, 1, backend=Timed())["tape"][0]
        assert (batch["started_at"], batch["finished_at"]) == ("2026-09-28T06:00:00+00:00", "2026-09-28T06:00:02+00:00")

    def test_a_backend_is_still_callable_as_a_bit_source(self):
        class One(BaseBackend):
            def run(self, circuit, shots, budget):
                return BackendRun(["1"] * shots, "one")

        assert One()(None, 2, None) == BackendRun(["1", "1"], "one")

    def test_extract_entropy_is_exported(self):
        assert vqrng.extract_entropy is extract_entropy
