import itertools

import pytest

import vqrng
from vqrng.core import bits_for_range, canonical_json, sha256_hex


def fake_source(values):
    """Bit source that replays ``values`` as fixed-width bitstrings."""
    stream = iter(values)

    def source(circuit, shots, budget):
        width = circuit.num_clbits
        chunk = list(itertools.islice(stream, shots))
        return [format(v, f"0{width}b") for v in chunk], "fake", 1.0

    return source


class TestResolveRange:
    def test_positional_bounds(self):
        assert vqrng.resolve_range(1, 100, None, False) == (1, 100)

    def test_negative_bounds(self):
        assert vqrng.resolve_range(-10, -5, None, False) == (-10, -5)

    @pytest.mark.parametrize("digits, expected", [(1, (1, 9)), (4, (1000, 9999)), (6, (100000, 999999))])
    def test_digits_without_pad(self, digits, expected):
        assert vqrng.resolve_range(None, None, digits, False) == expected

    @pytest.mark.parametrize("digits, expected", [(1, (0, 9)), (4, (0, 9999)), (6, (0, 999999))])
    def test_digits_with_pad(self, digits, expected):
        assert vqrng.resolve_range(None, None, digits, True) == expected

    @pytest.mark.parametrize(
        "args",
        [
            (1, 100, 6, False),     # bounds and digits
            (None, None, None, False),  # nothing
            (1, None, None, False),  # only min
            (None, 5, None, False),  # only max
            (10, 1, None, False),   # inverted
            (None, None, 0, False),  # zero digits
            (1, 100, None, True),   # pad without digits
        ],
    )
    def test_invalid(self, args):
        with pytest.raises(ValueError):
            vqrng.resolve_range(*args)


@pytest.mark.parametrize("size, bits", [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (100, 7), (900000, 20)])
def test_bits_for_range(size, bits):
    assert bits_for_range(size) == bits


class TestRejectionSampling:
    def test_rejects_out_of_range_candidates(self):
        # Range [1, 5] -> 3 bits; candidates 5, 6, 7 are out of range.
        evidence = vqrng.generate(1, 5, pool_size=3, _source=fake_source([7, 2, 5, 6, 0, 4]))
        items = evidence["items"]
        assert [i["number"] for i in items] == [3, 1, 5]
        assert items[0]["rejected"] == ["111"]
        assert items[1]["rejected"] == ["101", "110"]
        assert items[2]["rejected"] == []

    def test_pool_hash_covers_the_whole_payload(self):
        evidence = vqrng.generate(0, 7, pool_size=4, _source=fake_source(range(8)))
        body = {k: v for k, v in evidence.items() if k != "pool_hash"}
        assert evidence["pool_hash"] == sha256_hex(canonical_json(body))
        assert evidence["circuit"]["sha256"] == sha256_hex(evidence["circuit"]["qasm"])

    def test_evidence_metadata(self):
        evidence = vqrng.generate(1, 100, pool_size=2, _source=fake_source(range(10)))
        assert evidence["mode"] == "aer"
        assert evidence["backend"] == "fake"
        assert evidence["n_bits"] == 7
        assert evidence["range"] == {"min": 1, "max": 100, "size": 100}
        assert evidence["request"]["pool_size"] == 2


class TestFormatting:
    def test_pad_zero_fills(self):
        evidence = vqrng.generate(digits=6, pad=True, pool_size=2, _source=fake_source([4812, 7]))
        assert [i["formatted"] for i in evidence["items"]] == ["004812", "000007"]
        assert [i["number"] for i in evidence["items"]] == [4812, 7]

    def test_digits_without_pad_offsets_from_lower_bound(self):
        evidence = vqrng.generate(digits=6, _source=fake_source([0]))
        assert evidence["items"][0]["number"] == 100000
        assert evidence["items"][0]["formatted"] == "100000"


class TestValidation:
    def test_hardware_requires_runtime(self):
        with pytest.raises(ValueError, match="runtime_limit"):
            vqrng.generate(1, 10, mode="hardware")

    def test_unknown_mode(self):
        with pytest.raises(ValueError, match="mode"):
            vqrng.generate(1, 10, mode="gpu")

    def test_pool_size_must_be_positive(self):
        with pytest.raises(ValueError, match="pool_size"):
            vqrng.generate(1, 10, pool_size=0)

    def test_hardware_budget_exhaustion(self):
        # Each fake batch "uses" 1 second; every candidate is rejected.
        with pytest.raises(RuntimeError, match="budget"):
            vqrng.generate(0, 4, mode="hardware", runtime_limit=2,
                           _source=fake_source(itertools.repeat(7)))


class TestAer:
    def test_values_within_bounds(self):
        evidence = vqrng.generate(1, 6, pool_size=50)
        numbers = [i["number"] for i in evidence["items"]]
        assert len(numbers) == 50
        assert all(1 <= n <= 6 for n in numbers)
        assert evidence["backend"] == "aer_simulator"

    def test_padded_digits(self):
        evidence = vqrng.generate(digits=4, pad=True, pool_size=20)
        for item in evidence["items"]:
            assert len(item["formatted"]) == 4
            assert 0 <= int(item["formatted"]) <= 9999
