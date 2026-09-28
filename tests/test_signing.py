"""Ed25519 signatures over pool_hash, and Level B's checksum-only vs authentic reporting."""

import copy
import io
import json
import sys

import pytest

import vqrng
from vqrng import cli, signing
from vqrng.evidence import payload_hash
from vqrng.verifier.level_b import check_level_b

# RFC 8032 section 7.1, tests 1 and 2.
RFC_VECTORS = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "r",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]
KEY = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
POOL_HASH = "ab" * 32


def constant(circuit, shots, budget):
    return ["1" * circuit.num_clbits] * shots, "fake", 0.0


@pytest.fixture
def signed():
    return vqrng.generate(1, 6, pool_size=2, signing_key=KEY, _source=constant)


def reseal(evidence, mutate):
    copy_ = copy.deepcopy(evidence)
    mutate(copy_)
    copy_["pool_hash"] = payload_hash(copy_)
    return copy_


class TestVerifySignature:
    @pytest.mark.parametrize(("public_key", "message", "signature"), RFC_VECTORS)
    def test_rfc_8032_vectors(self, monkeypatch, public_key, message, signature):
        monkeypatch.setattr(signing, "SIGNATURE_CONTEXT", b"")
        assert signing.verify_signature(public_key, signature, message)
        assert not signing.verify_signature(public_key, signature, message + "x")

    def test_round_trip_with_the_cryptography_signer(self):
        signature, public_key = signing.sign_pool_hash(KEY, POOL_HASH)
        assert public_key == RFC_VECTORS[0][0] == signing.public_key_hex(KEY)
        assert signing.verify_signature(public_key, signature, POOL_HASH)
        assert not signing.verify_signature(public_key, signature, "cd" * 32)
        assert not signing.verify_signature(RFC_VECTORS[1][0], signature, POOL_HASH)

    def test_the_context_prevents_signing_raw_pool_hashes(self, monkeypatch):
        signature, public_key = signing.sign_pool_hash(KEY, POOL_HASH)
        monkeypatch.setattr(signing, "SIGNATURE_CONTEXT", b"")
        assert not signing.verify_signature(public_key, signature, POOL_HASH)

    @pytest.mark.parametrize("edit", [
        lambda key, sig: (key, sig[:-2]),
        lambda key, sig: (key[:-2], sig),
        lambda key, sig: (None, sig),
        lambda key, sig: (key, 7),
        lambda key, sig: (key, "zz" + sig[2:]),
        lambda key, sig: ("ff" * 32, sig),  # y >= p: not a point
        lambda key, sig: (key, "ff" * 32 + sig[64:]),  # R is not a point
        lambda key, sig: (key, sig[:64] + "ff" * 32),  # s >= L
        lambda key, sig: (key, sig[:2] + ("00" if sig[2:4] != "00" else "01") + sig[4:]),
    ])
    def test_malformed_or_altered_inputs_fail(self, edit):
        signature, public_key = signing.sign_pool_hash(KEY, POOL_HASH)
        key, sig = edit(public_key, signature)
        assert not signing.verify_signature(key, sig, POOL_HASH)

    def test_point_decoding(self):
        p = signing._P
        assert signing._recover_x(p, 0) is None
        assert signing._recover_x(1, 0) == 0 and signing._recover_x(1, 1) is None
        for y in range(2, 40):
            for sign in (0, 1):
                x = signing._recover_x(y, sign)
                if x is not None:
                    assert x % 2 == sign
                    assert (-x * x + y * y - 1 - signing._D * x * x * y * y) % p == 0
        assert any(signing._recover_x(y, 0) is None for y in range(2, 40))


class TestSigningKeys:
    def test_generated_keys_are_32_random_bytes(self):
        first, second = signing.generate_signing_key(), signing.generate_signing_key()
        assert len(bytes.fromhex(first)) == 32 and first != second

    @pytest.mark.parametrize("key", ["abc", "zz" * 32, ""])
    def test_bad_keys_are_rejected(self, key):
        with pytest.raises(ValueError, match="64 hex characters"):
            signing.sign_pool_hash(key, POOL_HASH)

    def test_signing_without_cryptography_is_a_clear_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.asymmetric.ed25519", None)
        with pytest.raises(RuntimeError, match=r"pip install vqrng\[sign\]"):
            signing.public_key_hex(KEY)

    def test_a_bad_key_stops_generation_before_any_job(self):
        def source(circuit, shots, budget):
            raise AssertionError("no job should run")

        with pytest.raises(ValueError, match="64 hex characters"):
            vqrng.generate(1, 6, signing_key="nope", _source=source)


class TestLevelBAssurance:
    def test_unsigned_evidence_is_checksum_only(self):
        result = vqrng.verify(vqrng.generate(1, 6, _source=constant))
        assert result.is_valid and result.level_b_assurance == "checksum-only"

    def test_signed_evidence_without_a_trusted_key_is_not_authentic(self, signed):
        assert signed["public_key"] == RFC_VECTORS[0][0]
        assert signed["pool_hash"] == payload_hash(signed)
        result = vqrng.verify(json.dumps(signed))
        assert result.is_valid and result.level_b_assurance == "signed-untrusted-key"

    @pytest.mark.parametrize("trusted", [[RFC_VECTORS[0][0]], [" " + RFC_VECTORS[0][0].upper() + "\n", "00" * 32]])
    def test_a_trusted_key_makes_it_authentic(self, signed, trusted):
        result = vqrng.verify(signed, trusted_keys=trusted)
        assert result.is_valid and result.level_b_assurance == "authentic"

    def test_an_untrusted_signer_fails_when_trust_is_required(self, signed):
        result = vqrng.verify(signed, trusted_keys=[RFC_VECTORS[1][0]])
        assert not result.is_valid and result.level_b_assurance is None
        assert result.levels["B"].errors == [f"public_key {RFC_VECTORS[0][0]} is not one of the trusted keys."]

    def test_unsigned_evidence_fails_when_trust_is_required(self):
        result = vqrng.verify(vqrng.generate(1, 6, _source=constant), trusted_keys=[RFC_VECTORS[0][0]])
        assert result.levels["B"].errors == ["the evidence is not signed, but a trusted key is required."]

    def test_an_empty_trust_list_requires_nothing(self, signed):
        assert vqrng.verify(signed, trusted_keys=[]).level_b_assurance == "signed-untrusted-key"

    def test_a_resealed_edit_passes_the_checksum_but_not_the_signature(self, signed):
        forged = reseal(signed, lambda e: e.update(generated_at="2020-01-01T00:00:00+00:00"))
        errors, assurance = check_level_b(forged)
        assert assurance is None
        assert errors == ["signature does not verify for pool_hash under public_key."]

    def test_a_forger_can_re_sign_with_their_own_key(self, signed):
        forged = reseal(signed, lambda e: e.update(generated_at="2020-01-01T00:00:00+00:00"))
        other = signing.generate_signing_key()
        forged["signature"], forged["public_key"] = signing.sign_pool_hash(other, forged["pool_hash"])
        assert check_level_b(forged) == ([], "signed-untrusted-key")
        assert check_level_b(forged, [RFC_VECTORS[0][0]])[1] is None

    @pytest.mark.parametrize(("mutate", "message"), [
        (lambda e: e.pop("public_key"), "signature and public_key must be present together."),
        (lambda e: e.pop("signature"), "signature and public_key must be present together."),
        (lambda e: e.update(signature="00" * 64), "signature does not verify"),
        (lambda e: e.update(public_key=RFC_VECTORS[1][0]), "signature does not verify"),
    ])
    def test_signature_fields_are_outside_the_hash_but_still_checked(self, signed, mutate, message):
        copy_ = copy.deepcopy(signed)
        mutate(copy_)
        assert copy_["pool_hash"] == payload_hash(copy_)
        errors, _ = check_level_b(copy_)
        assert errors == [message] or errors[0].startswith(message)

    def test_a_missing_pool_hash_fails_the_signature_too(self, signed):
        del signed["pool_hash"]
        errors, _ = check_level_b(signed)
        assert errors == ["pool_hash is missing.", "signature does not verify for pool_hash under public_key."]

    def test_partial_evidence_is_signed_too(self):
        def fail(circuit, shots, budget):
            raise RuntimeError("backend offline")

        with pytest.raises(vqrng.GenerationError) as exc:
            vqrng.generate(1, 6, signing_key=KEY, _source=fail)
        assert vqrng.verify(exc.value.evidence, trusted_keys=[RFC_VECTORS[0][0]]).level_b_assurance == "authentic"


class TestCli:
    def run(self, capsys, *argv, stdin=None, monkeypatch=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    @pytest.fixture(params=["utf-8", "utf-16"])
    def key_file(self, request, tmp_path):
        path = tmp_path / "signing.key"
        path.write_bytes((KEY + "\r\n").encode(request.param))
        return str(path)

    def test_sign_then_verify_with_and_without_trust(self, capsys, monkeypatch, key_file):
        code, out, _ = self.run(capsys, "-j", "--sign-key", key_file, "1", "6")
        assert code == 0
        assert json.loads(out)["public_key"] == RFC_VECTORS[0][0]

        _, report, _ = self.run(capsys, "verify", stdin=out, monkeypatch=monkeypatch)
        assert ("PASS: Level B (tamper-evident provenance) via Ed25519 signature from an untrusted key "
                "(self-consistent; pass --trusted-key to authenticate)") in report

        code, report, _ = self.run(capsys, "verify", "--trusted-key", RFC_VECTORS[0][0],
                                   stdin=out, monkeypatch=monkeypatch)
        assert code == 0
        assert "PASS: Level B (tamper-evident provenance) via Ed25519 signature from a trusted key (authentic)" in report

        code, report, _ = self.run(capsys, "verify", "--trusted-key", RFC_VECTORS[1][0],
                                   stdin=out, monkeypatch=monkeypatch)
        assert code == 1
        assert "FAIL: Level B (tamper-evident provenance)\n  - public_key" in report

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be"])
    def test_verify_reads_files_in_common_encodings(self, capsys, tmp_path, encoding):
        evidence = vqrng.generate(1, 6, _source=constant)
        text = json.dumps(evidence)
        data = text.encode(encoding)
        if encoding == "utf-16-be":
            data = b"\xfe\xff" + data
        path = tmp_path / "evidence.json"
        path.write_bytes(data)
        code, out, _ = self.run(capsys, "verify", str(path))
        assert code == 0 and "PASS: Level B" in out

    def test_verify_rejects_undecodable_files(self, capsys, tmp_path):
        path = tmp_path / "evidence.json"
        path.write_bytes(b"\xff\x00\xfe")
        code, _, err = self.run(capsys, "verify", str(path))
        assert code == 1 and "vqrng verify: error:" in err

    def test_missing_key_file(self, capsys, tmp_path):
        code, out, err = self.run(capsys, "--sign-key", str(tmp_path / "nope.key"), "1", "6")
        assert (code, out) == (1, "")
        assert "cannot read --sign-key" in err

    def test_bad_key_file(self, capsys, tmp_path):
        path = tmp_path / "bad.key"
        path.write_text("not hex", encoding="utf-8")
        code, out, err = self.run(capsys, "--sign-key", str(path), "1", "6")
        assert (code, out) == (1, "")
        assert "64 hex characters" in err
