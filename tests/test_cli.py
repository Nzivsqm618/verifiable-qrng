import json
import os

import pytest

import vqrng
from vqrng import cli
from vqrng.evidence import canonical_json


def fake_evidence(values):
    return {"items": [{"number": int(v), "formatted": v} for v in values], "pool_hash": "abc"}


@pytest.fixture
def calls(monkeypatch):
    """Replace vqrng.generate and record its keyword arguments."""
    recorded = []

    def fake_generate(**kwargs):
        recorded.append(kwargs)
        return fake_evidence(["741829", "938201"][: kwargs["pool_size"]])

    monkeypatch.setattr(cli.vqrng, "generate", fake_generate)
    return recorded


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


class TestPositionalBounds:
    def test_min_max_passed_through(self, calls, capsys):
        code, out, _ = run(capsys, "1", "100")
        assert code == 0
        assert calls[0]["min_val"] == 1 and calls[0]["max_val"] == 100
        assert calls[0]["digits"] is None

    def test_negative_bounds(self, calls, capsys):
        code, _, _ = run(capsys, "-10", "-5")
        assert code == 0
        assert (calls[0]["min_val"], calls[0]["max_val"]) == (-10, -5)

    @pytest.mark.parametrize("argv", [["5"], ["10", "1"], ["a", "b"], ["1", "2", "3"]])
    def test_bad_positionals(self, calls, capsys, argv):
        code, out, err = run(capsys, *argv)
        assert code == 1
        assert out == ""
        assert "error" in err
        assert calls == []


class TestDigits:
    @pytest.mark.parametrize("flag", ["-d", "--digits"])
    def test_digits_flag(self, calls, capsys, flag):
        code, _, _ = run(capsys, flag, "6")
        assert code == 0
        assert calls[0]["digits"] == 6
        assert calls[0]["pad"] is False
        assert calls[0]["min_val"] is None and calls[0]["max_val"] is None

    def test_pad(self, calls, capsys):
        code, _, _ = run(capsys, "-d", "6", "--pad")
        assert code == 0
        assert calls[0]["pad"] is True

    def test_pad_without_digits(self, calls, capsys):
        code, _, err = run(capsys, "--pad", "1", "100")
        assert code == 1
        assert "--pad" in err

    @pytest.mark.parametrize("value", ["0", "-1", "x"])
    def test_invalid_digits(self, calls, capsys, value):
        code, _, _ = run(capsys, "-d", value)
        assert code == 1

    def test_digits_and_bounds_conflict(self, calls, capsys):
        code, out, err = run(capsys, "-d", "6", "1", "100")
        assert code == 1
        assert out == ""
        assert "not both" in err
        assert calls == []

    def test_omission(self, calls, capsys):
        code, out, err = run(capsys)
        assert code == 1
        assert out == ""
        assert "MIN_VAL MAX_VAL or -d/--digits" in err


class TestOutputFormats:
    def test_default_newline_separated(self, calls, capsys):
        _, out, err = run(capsys, "-p", "2", "1", "999999")
        assert out == "741829\n938201\n"
        assert "generating" in err
        assert "does not cap" not in err

    def test_raw_space_separated(self, calls, capsys):
        _, out, _ = run(capsys, "-r", "-p", "2", "1", "999999")
        assert out == "741829 938201\n"

    def test_json(self, calls, capsys):
        _, out, _ = run(capsys, "--json", "1", "100")
        assert json.loads(out)["pool_hash"] == "abc"

    def test_json_and_raw_conflict(self, calls, capsys):
        code, out, _ = run(capsys, "-j", "-r", "1", "100")
        assert code == 1
        assert out == ""


class TestModeFlags:
    def test_default_simulator(self, calls, capsys):
        run(capsys, "1", "100")
        assert calls[0]["mode"] == "aer"

    def test_simulator_flag(self, calls, capsys):
        run(capsys, "-s", "1", "100")
        assert calls[0]["mode"] == "aer"

    @pytest.mark.parametrize("flags", [["-h", "-t", "300"], ["--hardware", "--runtime", "300"]])
    def test_hardware_with_runtime(self, calls, capsys, flags):
        code, _, _ = run(capsys, *flags, "-p", "2", "1", "100")
        assert code == 0
        assert calls[0]["mode"] == "hardware"
        assert calls[0]["runtime_limit"] == 300
        assert calls[0]["pool_size"] == 2

    def test_hardware_requires_runtime(self, calls, capsys):
        code, out, err = run(capsys, "-h", "1", "100")
        assert code == 1
        assert out == ""
        assert "--runtime" in err
        assert calls == []

    def test_simulator_and_hardware_conflict(self, calls, capsys):
        code, _, _ = run(capsys, "-s", "-h", "-t", "10", "1", "100")
        assert code == 1

    @pytest.mark.parametrize("value", ["0", "-3"])
    def test_invalid_pool(self, calls, capsys, value):
        code, _, _ = run(capsys, "-p", value, "1", "100")
        assert code == 1

    def test_combined_short_flags(self, calls, capsys):
        code, out, _ = run(capsys, "-sr", "-p", "2", "-d", "6", "--pad")
        assert code == 0
        assert out == "741829 938201\n"
        assert calls[0]["digits"] == 6 and calls[0]["pad"] is True

    def test_help_is_long_option_only(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        out = capsys.readouterr().out
        assert exc.value.code == 0
        assert "--hardware" in out
        assert "Not an IBM cap" in out


def test_runtime_warnings_stay_off_the_console(monkeypatch, capsys):
    seen = {}

    def fake_generate(**kwargs):
        seen["level"] = os.environ.get("QISKIT_IBM_RUNTIME_LOG_LEVEL")
        return fake_evidence(["4"])

    monkeypatch.delenv("QISKIT_IBM_RUNTIME_LOG_LEVEL", raising=False)
    monkeypatch.setattr(cli.vqrng, "generate", fake_generate)
    code, out, err = run(capsys, "1", "6")
    assert code == 0
    assert out == "4\n"
    assert seen["level"] == "ERROR"
    assert "qiskit_runtime_service" not in err
    assert "QISKIT_IBM_RUNTIME_LOG_LEVEL" not in os.environ


def test_user_runtime_log_level_is_kept(monkeypatch, capsys):
    seen = {}

    def fake_generate(**kwargs):
        seen["level"] = os.environ.get("QISKIT_IBM_RUNTIME_LOG_LEVEL")
        return fake_evidence(["4"])

    monkeypatch.setenv("QISKIT_IBM_RUNTIME_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(cli.vqrng, "generate", fake_generate)
    code, _, _ = run(capsys, "1", "6")
    assert code == 0
    assert seen["level"] == "DEBUG"


def test_generation_errors_go_to_stderr(monkeypatch, capsys):
    def boom(**kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(cli.vqrng, "generate", boom)
    code, out, err = run(capsys, "1", "100")
    assert code == 1
    assert out == ""
    assert "backend unavailable" in err


class TestEndToEndAer:
    def test_padded_tokens(self, capsys):
        code, out, _ = run(capsys, "-d", "6", "--pad", "-p", "5")
        lines = out.splitlines()
        assert code == 0
        assert len(lines) == 5
        assert all(len(line) == 6 and line.isdigit() for line in lines)

    def test_json_evidence(self, capsys):
        code, out, _ = run(capsys, "-j", "-p", "3", "1", "6")
        evidence = json.loads(out)
        assert code == 0
        assert all(1 <= item["number"] <= 6 for item in evidence["items"])

    def test_json_line_is_canonical(self, capsys):
        code, out, _ = run(capsys, "-s", "-j", "1", "100")
        assert code == 0
        evidence = json.loads(out)
        assert out == canonical_json(evidence) + "\n"
        assert vqrng.verify(evidence).is_valid
