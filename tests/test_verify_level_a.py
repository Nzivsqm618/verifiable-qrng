import copy
import io
import json

import pytest

import vqrng
from vqrng import cli
from vqrng.verifier.level_a import verify_level_a
from tests.test_generate import fake_source


@pytest.fixture
def evidence():
    # Range [1, 5] -> 3 bits; candidates 5, 6, 7 are rejected.
    return vqrng.generate(1, 5, pool_size=3, _source=fake_source([7, 2, 5, 6, 0, 4]))


@pytest.fixture
def padded():
    return vqrng.generate(digits=4, pad=True, pool_size=2, _source=fake_source([42, 16383, 9999]))


def tampered(evidence, mutate):
    copy_ = copy.deepcopy(evidence)
    mutate(copy_)
    return verify_level_a(copy_)


class TestValidEvidence:
    def test_fake_source(self, evidence):
        assert verify_level_a(evidence) == (True, [])

    def test_padded_digits(self, padded):
        assert verify_level_a(padded) == (True, [])

    def test_single_value_range(self):
        assert verify_level_a(vqrng.generate(7, 7, pool_size=2, _source=fake_source([0, 1, 0]))) == (True, [])

    def test_negative_range(self):
        ev = vqrng.generate(-10, -5, pool_size=4, _source=fake_source(range(8)))
        assert verify_level_a(ev) == (True, [])

    def test_survives_json_round_trip(self, evidence):
        assert verify_level_a(json.loads(json.dumps(evidence))) == (True, [])

    def test_aer(self):
        assert verify_level_a(vqrng.generate(1, 1000, pool_size=25)) == (True, [])

    def test_flat_evidence_shape(self):
        flat = {
            "min_val": 10, "max_val": 12, "n_bits": 2, "digits": None, "pad": False,
            "items": [{"number": 11, "bitstring": "01", "rejected": ["11"]}],
        }
        assert verify_level_a(flat) == (True, [])


class TestTampering:
    def test_modified_number(self, evidence):
        ok, errors = tampered(evidence, lambda e: e["items"][1].update(number=4))
        assert not ok
        assert errors == ["items[1]: number is 4, but bitstring '000' maps to 1."]

    def test_modified_bitstring(self, evidence):
        ok, errors = tampered(evidence, lambda e: e["items"][0].update(bitstring="011"))
        assert not ok
        assert "items[0]: number is 3, but bitstring '011' maps to 4." in errors

    def test_out_of_range_accepted_bitstring(self, evidence):
        ok, errors = tampered(evidence, lambda e: e["items"][0].update(bitstring="110"))
        assert not ok
        assert "should have been rejected" in errors[0]

    def test_in_range_rejected_candidate(self, evidence):
        ok, errors = tampered(evidence, lambda e: e["items"][1]["rejected"].__setitem__(0, "001"))
        assert not ok
        assert errors == [
            "items[1]: rejected[0] '001' decodes to 1, which is within range size 5 "
            "and should have been accepted."
        ]

    @pytest.mark.parametrize("bits", ["10", "1010", "1x1", 5, None])
    def test_malformed_bitstring(self, evidence, bits):
        ok, errors = tampered(evidence, lambda e: e["items"][0].update(bitstring=bits))
        assert not ok
        assert "is not a 3-bit binary string" in errors[0]

    def test_wrong_n_bits(self, evidence):
        ok, errors = tampered(evidence, lambda e: e.update(n_bits=4))
        assert not ok
        assert errors == ["n_bits is 4, but range size 5 requires 3 bits."]

    def test_modified_range(self, evidence):
        ok, errors = tampered(evidence, lambda e: e["range"].update(max=6, size=6))
        assert not ok
        assert any("should have been accepted" in err for err in errors)

    def test_modified_formatted(self, padded):
        ok, errors = tampered(padded, lambda e: e["items"][0].update(formatted="42"))
        assert not ok
        assert errors == ["items[0]: formatted is '42', expected '0042'."]

    def test_reports_every_error(self, evidence):
        def mutate(e):
            e["items"][0]["number"] = 99
            e["items"][2]["number"] = 99

        ok, errors = tampered(evidence, mutate)
        assert not ok
        assert len(errors) == 2

    @pytest.mark.parametrize("payload", [None, [], {}, {"range": {"min": 1, "max": 5}, "n_bits": 3, "items": []}])
    def test_malformed_payload(self, payload):
        ok, errors = verify_level_a(payload)
        assert not ok
        assert errors


class TestSdk:
    def test_verify_accepts_json_string(self, evidence):
        result = vqrng.verify(json.dumps(evidence))
        assert result.is_valid and result.errors == []

    def test_verify_invalid_json(self):
        result = vqrng.verify("{not json")
        assert not result.is_valid
        assert "not valid JSON" in result.errors[0]


class TestCli:
    def test_verify_file(self, evidence, tmp_path, capsys):
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        assert cli.main(["verify", str(path)]) == 0
        assert "PASS" in capsys.readouterr().out

    def test_verify_stdin_tampered(self, evidence, monkeypatch, capsys):
        evidence["items"][0]["number"] = 5
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(evidence)))
        assert cli.main(["verify"]) == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "items[0]: number is 5" in out

    def test_verify_missing_file(self, tmp_path, capsys):
        assert cli.main(["verify", str(tmp_path / "missing.json")]) == 1
        assert "error" in capsys.readouterr().err

    def test_verify_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["verify", "--help"])
        out, err = capsys.readouterr()
        assert exc.value.code == 0
        assert "usage: vqrng verify" in out
        assert err == ""

    def test_verify_with_no_file_on_a_terminal_shows_usage(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        assert cli.main(["verify"]) == 0
        out, _ = capsys.readouterr()
        assert "usage: vqrng verify" in out
