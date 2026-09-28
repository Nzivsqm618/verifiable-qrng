"""Cover the branches the rest of the suite never takes."""

import io
import itertools
import runpy
import sys
import warnings
from types import SimpleNamespace

import pytest

import vqrng
from vqrng import cli
from vqrng.core import build_circuit, hardware_source
from vqrng.verifier.level_a import verify_level_a
from tests.test_generate import fake_source


class _WriteOnly:
    def write(self, text):
        self.text = text


class _Reconfigure:
    def __init__(self, error):
        self.error = error

    def reconfigure(self, **kwargs):
        raise self.error


def _evidence(**overrides):
    payload = {
        "min_val": 0,
        "max_val": 1,
        "n_bits": 1,
        "items": [{"number": 0, "bitstring": "0", "rejected": []}],
    }
    payload.update(overrides)
    return payload


class TestCliBranches:
    def test_skips_streams_that_cannot_be_reconfigured(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", _WriteOnly())
        monkeypatch.setattr(sys, "stderr", _WriteOnly())
        cli._configure_stdio()

    def test_ignores_reconfigure_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", _Reconfigure(OSError("closed")))
        monkeypatch.setattr(sys, "stderr", _Reconfigure(ValueError("closed")))
        cli._configure_stdio()

    def test_write_flushes_when_it_can_and_skips_when_it_cannot(self):
        plain = _WriteOnly()
        cli._write(plain, "hi")
        cli._write(plain, "hi\n")
        assert plain.text == "hi\n"

        flushed = _WriteOnly()
        flushed.flushed = False
        flushed.flush = lambda: setattr(flushed, "flushed", True)
        cli._write(flushed, "ok")
        assert flushed.flushed

    def test_verify_reads_stdin_when_the_file_is_a_dash(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
        assert cli.main(["verify", "-"]) == 1
        assert "not valid JSON" in capsys.readouterr().out

    def test_verify_usage_error(self, capsys):
        assert cli.main(["verify", "--bogus"]) == 1
        assert "unrecognized" in capsys.readouterr().err

    def test_verify_interrupted_while_reading_stdin(self, monkeypatch, capsys):
        class Stdin:
            def read(self):
                raise KeyboardInterrupt

        monkeypatch.setattr(sys, "stdin", Stdin())
        assert cli.main(["verify", "-"]) == 1
        assert "interrupted" in capsys.readouterr().err

    def test_module_entry_point(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["vqrng", "0", "1"])
        monkeypatch.setattr(vqrng, "generate", lambda **kwargs: {"items": [{"formatted": "0"}]})
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*vqrng\.cli.*sys\.modules.*", category=RuntimeWarning)
            with pytest.raises(SystemExit) as exc:
                runpy.run_module("vqrng.cli", run_name="__main__")
        assert exc.value.code == 0


class TestCoreBranches:
    def test_sampling_stops_after_the_batch_limit(self, monkeypatch):
        monkeypatch.setattr("vqrng.core.MAX_BATCHES", 1)
        with pytest.raises(RuntimeError, match="did not converge after 1"):
            vqrng.generate(0, 0, _source=fake_source(itertools.repeat(1)))

    def test_generate_uses_the_hardware_backend(self, monkeypatch):
        def source(circuit, shots, budget):
            assert budget == 2
            return ["0"], "ibm_fake", 1.0

        monkeypatch.setattr("vqrng.core.hardware_source", source)
        evidence = vqrng.generate(0, 1, mode="hardware", runtime_limit=2)
        assert evidence["backend"] == "ibm_fake"
        assert evidence["quantum_seconds"] == 1.0

    @pytest.mark.parametrize(
        ("token", "budget", "usage", "expected_limit", "expected_used"),
        [
            ("secret", 2.9, 4, 2, 4.0),
            (None, None, None, None, 0.0),
        ],
    )
    def test_hardware_source(self, monkeypatch, token, budget, usage, expected_limit, expected_used):
        circuit = build_circuit(1)
        seen = {}

        class Service:
            def __init__(self, token=None):
                seen["token"] = token

            def least_busy(self, **kwargs):
                return SimpleNamespace(name="ibm_brisbane")

        class Sampler:
            def __init__(self, mode):
                self.options = SimpleNamespace()

            def run(self, circuits, shots):
                seen["limit"] = getattr(self.options, "max_execution_time", None)

                class Job:
                    def result(self):
                        bits = SimpleNamespace(get_bitstrings=lambda: ["0"])
                        return [SimpleNamespace(data={circuit.cregs[0].name: bits})]

                    def usage(self):
                        return usage

                return Job()

        monkeypatch.setattr(
            "qiskit.transpiler.preset_passmanagers.generate_preset_pass_manager",
            lambda **kwargs: SimpleNamespace(run=lambda circ: circ),
        )
        monkeypatch.setattr("qiskit_ibm_runtime.QiskitRuntimeService", Service)
        monkeypatch.setattr("qiskit_ibm_runtime.SamplerV2", Sampler)
        if token is None:
            monkeypatch.delenv("IBMQ_API_TOKEN", raising=False)
        else:
            monkeypatch.setenv("IBMQ_API_TOKEN", token)

        bitstrings, name, used = hardware_source(circuit, 8, budget)
        assert bitstrings == ["0"]
        assert name == "ibm_brisbane"
        assert used == expected_used
        assert seen["limit"] == expected_limit
        assert seen["token"] == token


class TestVerifierBranches:
    def test_item_must_be_an_object(self):
        ok, errors = verify_level_a(_evidence(items=["bad"]))
        assert not ok
        assert "expected an object" in errors[0]

    def test_index_must_match_position(self):
        item = {"index": 4, "number": 0, "bitstring": "0", "rejected": []}
        ok, errors = verify_level_a(_evidence(items=[item]))
        assert not ok
        assert "index is 4" in errors[0]

    def test_rejected_must_be_a_list(self):
        item = {"number": 0, "bitstring": "0", "rejected": "1"}
        ok, errors = verify_level_a(_evidence(items=[item]))
        assert not ok
        assert "must be a list" in errors[0]

    def test_malformed_rejected_candidate(self):
        item = {"number": 0, "bitstring": "0", "rejected": [None]}
        ok, errors = verify_level_a(_evidence(items=[item]))
        assert not ok
        assert "not a 1-bit binary string" in errors[0]

    def test_minimum_above_maximum(self):
        ok, errors = verify_level_a({"range": {"min": 2, "max": 1}, "n_bits": 1, "items": [{}]})
        assert not ok
        assert "greater than max" in errors[0]

    def test_only_one_bound_is_an_integer(self):
        ok, errors = verify_level_a({"range": {"min": 0, "max": "1"}, "n_bits": 1, "items": [{}]})
        assert not ok
        assert "Range bounds" in errors[0]

    def test_recorded_size_disagrees_with_the_bounds(self):
        ok, errors = verify_level_a(_evidence(range={"min": 0, "max": 1, "size": 9}))
        assert not ok
        assert "Range size is 9" in errors[0]

    def test_pad_requires_a_positive_digit_count(self):
        ok, errors = verify_level_a(_evidence(digits=0, pad=True))
        assert not ok
        assert "pad is set but digits is 0" in errors[0]

    def test_n_bits_of_zero_is_rejected(self):
        ok, errors = verify_level_a(_evidence(n_bits=0))
        assert not ok
        assert "n_bits" in errors[0]

    def test_items_must_be_a_list(self):
        ok, errors = verify_level_a(_evidence(items=None))
        assert not ok
        assert "items" in errors[0]
