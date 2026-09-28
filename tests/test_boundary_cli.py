"""Boundary-value tests for the CLI: the last valid argument and the first invalid one."""

import json

import pytest

import vqrng
from vqrng import cli


def fake_evidence(values):
    return {"items": [{"number": int(v), "formatted": v} for v in values], "pool_hash": "abc"}


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_generate(**kwargs):
        recorded.append(kwargs)
        return fake_evidence(["0"] * kwargs["pool_size"])

    monkeypatch.setattr(cli.vqrng, "generate", fake_generate)
    return recorded


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


class TestBoundArguments:
    @pytest.mark.parametrize("argv", [["0", "0"], ["5", "5"], ["-1", "-1"]])
    def test_min_equal_max_is_accepted(self, calls, capsys, argv):
        code, _, _ = run(capsys, *argv)
        assert code == 0
        assert calls[0]["min_val"] == calls[0]["max_val"] == int(argv[0])

    @pytest.mark.parametrize("argv", [["1", "0"], ["0", "-1"], ["-1", "-2"]])
    def test_min_one_past_max_is_rejected(self, calls, capsys, argv):
        code, out, err = run(capsys, *argv)
        assert code == 1
        assert out == ""
        assert "must be <=" in err
        assert calls == []

    def test_one_digit_is_the_minimum(self, calls, capsys):
        code, _, _ = run(capsys, "-d", "1")
        assert code == 0
        assert calls[0]["digits"] == 1
        assert calls[0]["pad"] is False

    def test_one_digit_with_pad(self, calls, capsys):
        code, _, _ = run(capsys, "-d", "1", "--pad")
        assert code == 0
        assert calls[0]["digits"] == 1
        assert calls[0]["pad"] is True

    @pytest.mark.parametrize("digits", ["0", "-1"])
    def test_digits_below_one_are_rejected(self, calls, capsys, digits):
        code, out, err = run(capsys, "-d", digits)
        assert code == 1
        assert out == ""
        assert "must be >= 1" in err
        assert calls == []

    def test_pool_of_one_is_the_minimum(self, calls, capsys):
        code, out, _ = run(capsys, "-p", "1", "0", "1")
        assert code == 0
        assert calls[0]["pool_size"] == 1
        assert out == "0\n"

    def test_pool_of_one_is_the_default(self, calls, capsys):
        code, out, _ = run(capsys, "3", "3")
        assert code == 0
        assert calls[0]["pool_size"] == 1
        assert out.count("\n") == 1

    @pytest.mark.parametrize("pool", ["0", "-1"])
    def test_pool_below_one_is_rejected(self, calls, capsys, pool):
        code, out, err = run(capsys, "-p", pool, "0", "1")
        assert code == 1
        assert out == ""
        assert "must be >= 1" in err
        assert calls == []


class TestRuntimeBounds:
    def test_one_second_passes_the_parser_and_fails_in_the_sdk(self, capsys):
        # The flag accepts any integer >= 1; generation itself requires >= 2.
        code, out, err = run(capsys, "-h", "-t", "1", "0", "1")
        assert code == 1
        assert out == ""
        assert "runtime_limit must be >= 2" in err

    def test_zero_seconds_is_rejected_by_the_parser(self, calls, capsys):
        code, out, err = run(capsys, "-h", "-t", "0", "0", "1")
        assert code == 1
        assert out == ""
        assert "must be >= 1" in err
        assert calls == []

    def test_two_seconds_is_the_minimum_accepted_budget(self, calls, capsys):
        code, _, _ = run(capsys, "-h", "-t", "2", "0", "1")
        assert code == 0
        assert calls[0]["mode"] == "hardware"
        assert calls[0]["runtime_limit"] == 2


class TestEndToEndBounds:
    def test_single_value_range(self, capsys):
        code, out, _ = run(capsys, "-j", "4", "4")
        evidence = json.loads(out)
        assert code == 0
        assert evidence["range"] == {"min": 4, "max": 4, "size": 1}
        assert evidence["n_bits"] == 1
        assert evidence["items"][0]["number"] == 4
        assert vqrng.verify(evidence).is_valid

    def test_one_digit_token(self, capsys):
        code, out, _ = run(capsys, "-d", "1")
        assert code == 0
        assert out.strip().isdigit()
        assert 1 <= int(out) <= 9

    def test_one_digit_padded_token(self, capsys):
        code, out, _ = run(capsys, "-d", "1", "--pad")
        assert code == 0
        assert len(out.strip()) == 1
        assert out.strip().isdigit()

    def test_pool_of_one_on_the_simulator(self, capsys):
        code, out, _ = run(capsys, "-p", "1", "0", "1")
        assert code == 0
        assert out.strip() in {"0", "1"}
