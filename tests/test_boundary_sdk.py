"""Boundary-value tests for the SDK: the last valid input and the first invalid one."""

import pytest

import vqrng
from vqrng.core import bits_for_range
from tests.test_generate import fake_source

pytestmark = pytest.mark.usefixtures("passthrough_extractor")


class TestRangeBounds:
    @pytest.mark.parametrize(
        "low, high",
        [(0, 0), (1, 1), (-1, -1), (-5, -5)],
    )
    def test_min_equal_max_is_a_one_value_range(self, low, high):
        evidence = vqrng.generate(low, high, pool_size=1, _source=fake_source([1, 0]))
        assert evidence["range"] == {"min": low, "max": high, "size": 1}
        assert evidence["n_bits"] == 1
        assert evidence["items"][0]["number"] == low
        assert evidence["items"][0]["rejected"] == ["1"]
        assert vqrng.verify(evidence).is_valid

    @pytest.mark.parametrize("low, high", [(1, 0), (0, -1), (-1, -2)])
    def test_min_one_past_max_is_rejected(self, low, high):
        with pytest.raises(ValueError, match="must be <="):
            vqrng.generate(low, high)

    def test_candidate_just_inside_maps_to_max_and_just_outside_is_rejected(self):
        # [10, 18] has size 9, so 4 bits. 8 is the last accepted candidate; 9 is the first rejected.
        evidence = vqrng.generate(10, 18, pool_size=2, _source=fake_source([9, 0, 8]))
        items = evidence["items"]
        assert [item["number"] for item in items] == [10, 18]
        assert items[0]["rejected"] == ["1001"]
        assert items[1]["bitstring"] == "1000"
        assert vqrng.verify(evidence).is_valid

    def test_power_of_two_range_accepts_every_bitstring(self):
        # Size 8 uses 3 bits, so 0..7 are all in range and nothing can be rejected.
        evidence = vqrng.generate(0, 7, pool_size=8, _source=fake_source(range(8)))
        assert evidence["n_bits"] == 3
        assert [item["number"] for item in evidence["items"]] == list(range(8))
        assert all(item["rejected"] == [] for item in evidence["items"])

    def test_one_past_power_of_two_rejects_the_next_candidate(self):
        # Size 9 needs 4 bits. 8 maps to the max; 9 is the first out-of-range value.
        evidence = vqrng.generate(0, 8, pool_size=2, _source=fake_source([9, 8, 0]))
        assert evidence["n_bits"] == 4
        assert evidence["items"][0]["number"] == 8
        assert evidence["items"][0]["rejected"] == ["1001"]
        assert evidence["items"][1]["number"] == 0

    def test_range_crossing_zero(self):
        # [-1, 1] has size 3. Candidates 0, 1, 2 map to -1, 0, 1; 3 is rejected.
        evidence = vqrng.generate(-1, 1, pool_size=3, _source=fake_source([3, 0, 1, 2]))
        assert [item["number"] for item in evidence["items"]] == [-1, 0, 1]
        assert evidence["items"][0]["rejected"] == ["11"]


class TestDigitBounds:
    def test_one_digit_spans_1_to_9(self):
        evidence = vqrng.generate(digits=1, pool_size=2, _source=fake_source([9, 0, 8]))
        assert evidence["range"] == {"min": 1, "max": 9, "size": 9}
        assert [item["number"] for item in evidence["items"]] == [1, 9]
        assert [item["formatted"] for item in evidence["items"]] == ["1", "9"]
        assert evidence["items"][0]["rejected"] == ["1001"]

    def test_one_digit_with_pad_spans_0_to_9(self):
        evidence = vqrng.generate(digits=1, pad=True, pool_size=2, _source=fake_source([10, 0, 9]))
        assert evidence["range"] == {"min": 0, "max": 9, "size": 10}
        assert [item["formatted"] for item in evidence["items"]] == ["0", "9"]
        assert evidence["items"][0]["rejected"] == ["1010"]

    def test_two_digit_pad_crosses_the_tens_boundary(self):
        # 0, 9, 10, and 99 are the edges; 100 is the first value past the range.
        evidence = vqrng.generate(
            digits=2, pad=True, pool_size=4, _source=fake_source([100, 0, 9, 10, 99])
        )
        assert [item["formatted"] for item in evidence["items"]] == ["00", "09", "10", "99"]
        assert evidence["items"][0]["rejected"] == ["1100100"]
        assert vqrng.verify(evidence).is_valid

    @pytest.mark.parametrize("digits", [0, -1])
    def test_digits_below_one_are_rejected(self, digits):
        with pytest.raises(ValueError, match="digits"):
            vqrng.generate(digits=digits)


class TestPoolAndRuntimeBounds:
    def test_pool_of_one_is_the_minimum(self):
        evidence = vqrng.generate(0, 1, pool_size=1, _source=fake_source([0]))
        assert len(evidence["items"]) == 1

    @pytest.mark.parametrize("pool_size", [0, -1])
    def test_pool_below_one_is_rejected(self, pool_size):
        with pytest.raises(ValueError, match="pool_size"):
            vqrng.generate(0, 1, pool_size=pool_size)

    def test_runtime_of_two_seconds_is_the_minimum(self):
        evidence = vqrng.generate(
            0, 1, mode="hardware", runtime_limit=2, pool_size=1, _source=fake_source([0])
        )
        assert evidence["quantum_seconds"] == 1.0
        assert evidence["items"][0]["number"] == 0

    def test_spending_the_whole_budget_still_succeeds_when_the_pool_fills(self):
        def source(circuit, shots, budget):
            assert budget == 2
            return ["0"], "fake", 2.0

        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=2, _source=source)
        assert evidence["quantum_seconds"] == 2.0

    def test_exhausting_the_budget_without_a_value_fails(self):
        def source(circuit, shots, budget):
            return ["1"], "fake", 2.0

        with pytest.raises(RuntimeError, match="budget"):
            vqrng.generate(0, 0, mode="hardware", runtime_limit=2, _source=source)

    @pytest.mark.parametrize("runtime_limit", [0, 1])
    def test_runtime_below_two_seconds_is_rejected(self, runtime_limit):
        with pytest.raises(ValueError, match="runtime_limit"):
            vqrng.generate(0, 1, mode="hardware", runtime_limit=runtime_limit)


class TestBitWidthBoundaries:
    @pytest.mark.parametrize(
        "size, bits",
        [(1, 1), (2, 1), (3, 2), (2**10 - 1, 10), (2**10, 10), (2**10 + 1, 11)],
    )
    def test_bit_width_steps_at_powers_of_two(self, size, bits):
        assert bits_for_range(size) == bits


class TestVerificationBounds:
    def test_accepting_the_first_out_of_range_candidate_fails(self):
        evidence = vqrng.generate(0, 4, pool_size=1, _source=fake_source([4]))
        evidence["items"][0]["bitstring"] = "101"  # 5 == range size
        result = vqrng.verify(evidence)
        assert not result.is_valid
        assert "should have been rejected" in result.errors[0]

    def test_rejecting_the_last_in_range_candidate_fails(self):
        evidence = vqrng.generate(0, 4, pool_size=1, _source=fake_source([5, 0]))
        evidence["items"][0]["rejected"] = ["100"]  # 4 == range size - 1
        result = vqrng.verify(evidence)
        assert not result.is_valid
        assert "should have been accepted" in result.errors[0]
