"""QSeed: a verified 32-byte seed, expanded locally with PCG64."""

import copy
import io
import json

import numpy as np
import pytest

import vqrng
from vqrng import cli
from vqrng.backends import BackendRun, BaseBackend
from vqrng.evidence import payload_hash
from vqrng.verifier.level_a import verify_level_a
from vqrng.verifier.level_b import verify_level_b
from tests.test_health import alternating, stuck

KEY = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUBLIC_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


class Qpu(BaseBackend):
    """A predictable stand-in for IBM hardware; the health tests pass and the jobs have ids."""

    def __init__(self, invert=False):
        self.invert = invert

    def run(self, circuit, shots, budget):
        bits, _, _ = alternating(circuit, shots, budget)
        if self.invert:
            bits = [b.translate(str.maketrans("01", "10")) for b in bits]
        return BackendRun(bits, "ibm_fez", quantum_seconds=1.0, job_id="job-1")


def hardware_seed(invert=False, **kwargs):
    return vqrng.core.collect_seed(mode="hardware", runtime_limit=60, backend=Qpu(invert), **kwargs)


def reseal(evidence, mutate):
    edited = copy.deepcopy(evidence)
    mutate(edited)
    edited["pool_hash"] = payload_hash(edited)
    return edited


@pytest.fixture(scope="module")
def aer_seed():
    return vqrng.collect_seed()


@pytest.fixture
def seed():
    return hardware_seed()


class TestCollectSeed:
    def test_a_seed_record_is_32_bytes_over_0_255(self, seed):
        assert seed["kind"] == "seed" and seed["mode"] == "hardware"
        assert (seed["range"], seed["n_bits"], seed["request"]["pool_size"]) == (
            {"min": 0, "max": 255, "size": 256}, 8, 32,
        )
        assert seed["expander"] == "numpy.random.PCG64"
        assert seed["seed"] == bytes(item["number"] for item in seed["items"]).hex()
        assert len(bytes.fromhex(seed["seed"])) == 32
        assert all(item["rejected"] == [] for item in seed["items"])
        assert seed["health"]["apt"]["passed"] is True

    def test_it_verifies_like_any_evidence(self, seed):
        result = vqrng.verify(seed)
        assert result.is_valid, result.errors

    def test_the_aer_seed_is_marked_as_simulated(self, aer_seed):
        assert aer_seed["mode"] == "aer" and vqrng.verify(aer_seed).is_valid

    def test_pool_records_are_kind_pool(self):
        assert vqrng.generate(1, 6, _source=alternating)["kind"] == "pool"

    def test_a_failed_seed_job_leaves_no_seed(self):
        with pytest.raises(vqrng.GenerationError, match="repetition count test failed") as exc:
            vqrng.collect_seed(_source=stuck("1"))
        evidence = exc.value.evidence
        assert evidence["seed"] is None and evidence["status"] == "partial"
        assert vqrng.verify(evidence).is_valid

    def test_chsh_and_a_signature_apply_to_the_seed_job(self):
        evidence = vqrng.collect_seed(chsh=True, signing_key=KEY)
        result = vqrng.verify(evidence, trusted_keys=[PUBLIC_KEY], require_chsh=True)
        assert result.level_b_assurance == "authentic"
        assert result.levels["C"].status != "skipped"


class TestQSeed:
    def test_the_same_record_gives_the_same_stream(self, seed):
        first = vqrng.QSeed(seed).integers(1, 100, size=1000)
        second = vqrng.QSeed(json.dumps(seed)).integers(1, 100, size=1000)
        assert np.array_equal(first, second)

    def test_another_seed_gives_another_stream(self, seed):
        other = hardware_seed(invert=True)
        assert other["seed"] != seed["seed"]
        assert not np.array_equal(vqrng.QSeed(seed).integers(1, 100, size=100),
                                  vqrng.QSeed(other).integers(1, 100, size=100))

    def test_the_generator_is_pcg64_seeded_by_the_seed_bytes(self, seed):
        rng = vqrng.QSeed(seed)
        expected = np.random.Generator(np.random.PCG64(int.from_bytes(bytes.fromhex(seed["seed"]), "big")))
        assert np.array_equal(rng.integers(0, 9, size=50), expected.integers(0, 9, size=50, endpoint=True))
        assert rng.seed == bytes.fromhex(seed["seed"]) and rng.pool_hash == seed["pool_hash"]
        assert isinstance(rng.generator.bit_generator, np.random.PCG64)

    def test_bounds_are_inclusive(self, seed):
        values = vqrng.QSeed(seed).integers(0, 1, size=1000)
        assert set(values.tolist()) == {0, 1}
        assert vqrng.QSeed(seed).integers(5, 5) == 5

    def test_a_reversed_range_is_rejected(self, seed):
        with pytest.raises(ValueError, match=r"low \(2\) must be <= high \(1\)"):
            vqrng.QSeed(seed).integers(2, 1)

    def test_a_simulator_seed_needs_explicit_permission(self, aer_seed):
        with pytest.raises(vqrng.QSeedError, match="mode 'aer', not IBM hardware"):
            vqrng.QSeed(aer_seed)
        assert vqrng.QSeed(aer_seed, allow_simulator=True).integers(1, 6) in range(1, 7)

    @pytest.mark.parametrize(("evidence", "message"), [
        ("{", "not valid JSON"),
        ("[]", "must be an object, got list"),
    ])
    def test_unreadable_records(self, evidence, message):
        with pytest.raises(vqrng.QSeedError, match=message):
            vqrng.QSeed(evidence)

    def test_a_pool_record_is_not_a_seed(self):
        with pytest.raises(vqrng.QSeedError, match="not a seed record \\(kind is 'pool'\\)"):
            vqrng.QSeed(vqrng.generate(1, 6, _source=alternating))

    def test_an_edited_record_is_refused(self, seed):
        seed["seed"] = "00" * 32
        with pytest.raises(vqrng.QSeedError, match="pool_hash does not match the seed record"):
            vqrng.QSeed(seed)

    @pytest.mark.parametrize(("mutate", "message"), [
        (lambda e: e.update(expander="random.Random"), "expander is 'random.Random'"),
        (lambda e: e.update(seed=None), "holds no complete 32-byte seed"),
        (lambda e: e.update(seed="zz" * 32), "holds no complete 32-byte seed"),
        (lambda e: e.update(seed="00" * 31), "holds no complete 32-byte seed"),
        (lambda e: e.update(status="partial"), "status is 'partial'"),
    ])
    def test_a_resealed_record_must_still_hold_a_seed(self, seed, mutate, message):
        with pytest.raises(vqrng.QSeedError, match=message):
            vqrng.QSeed(reseal(seed, mutate))

    def test_qseed_error_is_a_value_error(self):
        assert issubclass(vqrng.QSeedError, ValueError)


class TestVerifySeedRecord:
    def test_an_edited_seed_fails_level_a(self, seed):
        edited = reseal(seed, lambda e: e.update(seed="00" * 32))
        ok, errors = verify_level_a(edited)
        assert errors == [f"seed is {'00' * 32!r}, but the items give {seed['seed']!r}."]

    def test_the_seed_range_is_fixed(self, seed):
        def mutate(e):
            e["request"]["max_val"] = e["range"]["max"] = 254
            e["range"]["size"] = 255

        ok, errors = verify_level_a(reseal(seed, mutate))
        assert errors == ["a seed record must use the range [0, 255], got [0, 254]."]

    def test_the_expander_is_fixed(self, seed):
        ok, errors = verify_level_a(reseal(seed, lambda e: e.update(expander="mt19937")))
        assert errors == ["expander is 'mt19937', expected 'numpy.random.PCG64'."]

    def test_a_partial_record_has_no_seed(self):
        with pytest.raises(vqrng.GenerationError) as exc:
            vqrng.collect_seed(_source=stuck("0"))
        ok, errors = verify_level_a(reseal(exc.value.evidence, lambda e: e.update(seed="00" * 32)))
        assert errors == ["seed must be null in a partial seed record."]

    def test_a_completed_record_needs_32_bytes(self, seed):
        ok, errors = verify_level_a(reseal(seed, lambda e: e["items"].pop()))
        assert errors == ["a completed seed record needs 32 byte values, got 31 items."]

    def test_non_object_items_are_not_bytes(self, seed):
        def mutate(e):
            e["items"][-1] = "bad"

        ok, errors = verify_level_a(reseal(seed, mutate))
        assert errors[-1] == "a completed seed record needs 32 byte values, got 32 items."

    def test_kind_is_required(self, seed):
        ok, errors = verify_level_b(reseal(seed, lambda e: e.pop("kind")))
        assert errors == ["kind is None, expected one of ('pool', 'seed')."]


class TestSeedCli:
    def run(self, capsys, *argv):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    def test_simulator_seed_prints_canonical_json(self, capsys):
        code, out, err = self.run(capsys, "seed", "-s")
        assert code == 0
        evidence = json.loads(out)
        assert evidence["kind"] == "seed" and evidence["mode"] == "aer"
        assert out == vqrng.evidence.canonical_json(evidence) + "\n"
        assert "the simulator is a PRNG; expanding this seed needs --allow-simulator" in err

    def test_hardware_seed(self, capsys, monkeypatch):
        calls = []

        def fake(**kwargs):
            calls.append(kwargs)
            return hardware_seed()

        monkeypatch.setattr(vqrng, "collect_seed", fake)
        code, out, err = self.run(capsys, "seed", "-h", "-t", "30", "--backend", "ibm_fez", "--chsh")
        assert code == 0 and json.loads(out)["mode"] == "hardware"
        assert calls == [{"mode": "hardware", "runtime_limit": 30, "backend": "ibm_fez",
                          "chsh": True, "signing_key": None}]
        assert "warning: -t 30 does not cap what IBM bills" in err

    def test_a_signed_seed(self, capsys, tmp_path):
        key = tmp_path / "signing.key"
        key.write_text(KEY)
        code, out, _ = self.run(capsys, "seed", "-s", "--sign-key", str(key))
        assert code == 0 and json.loads(out)["public_key"] == PUBLIC_KEY

    @pytest.mark.parametrize(("argv", "message"), [
        ([], "one of the arguments -s/--simulator -h/--hardware is required"),
        (["-h"], "-t/--runtime is required with -h/--hardware"),
        (["-s", "--backend", "ibm_fez"], "--backend requires -h/--hardware"),
    ])
    def test_usage_errors(self, capsys, argv, message):
        code, out, err = self.run(capsys, "seed", *argv)
        assert code == 1 and out == ""
        assert err.startswith("vqrng seed: error: ") and message in err

    def test_an_unreadable_key_file(self, capsys, tmp_path):
        code, _, err = self.run(capsys, "seed", "-s", "--sign-key", str(tmp_path / "missing.key"))
        assert code == 1 and "vqrng seed: error: cannot read --sign-key:" in err

    def test_a_bad_key(self, capsys, tmp_path):
        key = tmp_path / "signing.key"
        key.write_text("nope")
        code, out, err = self.run(capsys, "seed", "-s", "--sign-key", str(key))
        assert code == 1 and out == "" and "64 hex characters" in err

    def test_a_failed_seed_job_prints_partial_json(self, capsys, monkeypatch):
        monkeypatch.setattr(vqrng, "collect_seed", lambda **kw: vqrng.core.collect_seed(_source=stuck("0")))
        code, out, err = self.run(capsys, "seed", "-s")
        assert code == 1
        assert json.loads(out)["seed"] is None
        assert "vqrng seed: error: repetition count test failed" in err

    def test_interrupt(self, capsys, monkeypatch):
        def interrupted(**kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(vqrng, "collect_seed", interrupted)
        code, out, err = self.run(capsys, "seed", "-s")
        assert code == 1 and out == "" and "vqrng seed: interrupted." in err

    def test_help(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["seed", "--help"])
        assert "Anyone holding the evidence can replay the expanded stream." in capsys.readouterr().out


class TestExpandCli:
    @pytest.fixture
    def seed_file(self, seed, tmp_path):
        path = tmp_path / "seed.json"
        path.write_text(json.dumps(seed))
        return str(path)

    def run(self, capsys, *argv):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    def test_prints_the_qseed_stream(self, seed, seed_file, capsys):
        code, out, _ = self.run(capsys, "expand", seed_file, "-p", "5", "1", "100")
        assert code == 0
        assert out == "\n".join(map(str, vqrng.QSeed(seed).integers(1, 100, size=5).tolist())) + "\n"

    def test_raw_output_and_the_default_of_one(self, seed_file, capsys):
        code, out, _ = self.run(capsys, "expand", seed_file, "-r", "-p", "3", "1", "6")
        assert code == 0 and len(out.split()) == 3 and out.count("\n") == 1
        code, out, _ = self.run(capsys, "expand", seed_file, "1", "6")
        assert code == 0 and out.strip() in {"1", "2", "3", "4", "5", "6"}

    def test_reads_stdin(self, seed, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(seed)))
        code, out, _ = self.run(capsys, "expand", "-", "1", "6")
        assert code == 0 and out.strip().isdigit()

    def test_reads_a_utf16_file_from_powershell(self, seed, tmp_path, capsys):
        path = tmp_path / "seed.json"
        path.write_bytes(json.dumps(seed).encode("utf-16"))
        assert self.run(capsys, "expand", str(path), "1", "6")[0] == 0

    def test_a_simulator_seed_needs_the_flag(self, aer_seed, tmp_path, capsys):
        path = tmp_path / "seed.json"
        path.write_text(json.dumps(aer_seed))
        code, out, err = self.run(capsys, "expand", str(path), "1", "6")
        assert code == 1 and out == "" and "--allow-simulator" in err
        assert self.run(capsys, "expand", str(path), "--allow-simulator", "1", "6")[0] == 0

    def test_a_truncated_file_is_refused(self, seed_file, capsys):
        with open(seed_file, encoding="utf-8") as handle:
            text = handle.read()
        with open(seed_file, "w", encoding="utf-8") as handle:
            handle.write(text[:-40])
        code, _, err = self.run(capsys, "expand", seed_file, "1", "6")
        assert code == 1 and "vqrng expand: error: the seed record is not valid JSON" in err

    def test_a_missing_file(self, tmp_path, capsys):
        code, _, err = self.run(capsys, "expand", str(tmp_path / "none.json"), "1", "6")
        assert code == 1 and err.startswith("vqrng expand: error: ")

    @pytest.mark.parametrize(("argv", "message"), [
        (["5"], "the following arguments are required: MAX_VAL"),
        (["10", "1"], "MIN_VAL (10) must be <= MAX_VAL (1)"),
        (["1", "6", "-p", "0"], "must be >= 1"),
    ])
    def test_usage_errors(self, seed_file, capsys, argv, message):
        code, out, err = self.run(capsys, "expand", seed_file, *argv)
        assert code == 1 and out == "" and message in err

    def test_help(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["expand", "--help"])
        assert "not for secrets" in " ".join(capsys.readouterr().out.split())

    def test_main_help_names_the_subcommands(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert "'vqrng seed' and 'vqrng expand'" in out and "not for secrets" in out
