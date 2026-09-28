"""HMAC-SHA256 conditioning of raw measurement bits.

Raw shots are concatenated into one bit stream. Every 512 raw bits are
conditioned by HMAC-SHA256 under a fixed public key (the salt) into 256
output bits, the construction NIST SP 800-90B lists as a vetted conditioning
component. Rejection sampling then reads candidates from the conditioned
stream. The 2:1 ratio assumes the raw source carries at least 0.5 bits of
min-entropy per measured bit; that is not estimated or health-tested here.

Standard library only, so the verifier can replay it offline.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

EXTRACTOR_SALT = b"vqrng-v1-extractor"
EXTRACTOR_INPUT_BITS = 512
EXTRACTOR_OUTPUT_BITS = 256
EXTRACTOR = {
    "name": "hmac-sha256",
    "salt": EXTRACTOR_SALT.hex(),
    "input_bits": EXTRACTOR_INPUT_BITS,
    "output_bits": EXTRACTOR_OUTPUT_BITS,
}


def extract_entropy(raw_bitstring: str, salt: bytes = EXTRACTOR_SALT) -> bytes:
    """Return the 32-byte HMAC-SHA256 of ``raw_bitstring`` keyed with ``salt``."""
    if not isinstance(raw_bitstring, str) or not raw_bitstring or set(raw_bitstring) - {"0", "1"}:
        raise ValueError(f"raw_bitstring must be a non-empty string of 0s and 1s, got {raw_bitstring!r}.")
    return hmac.new(salt, raw_bitstring.encode("ascii"), hashlib.sha256).digest()


def digest_bits(digest: bytes) -> str:
    return "".join(format(byte, "08b") for byte in digest)


def condition_block(block: str) -> str:
    """Condition one ``EXTRACTOR_INPUT_BITS`` raw block into its output bits."""
    return digest_bits(extract_entropy(block))


class Conditioner:
    """Turns raw shots into fixed-width candidates from the conditioned stream.

    Raw bits short of a full block, and conditioned bits short of a full
    candidate, are carried into the next ``feed``.
    """

    def __init__(self, width: int) -> None:
        self.width = width
        self._raw = ""
        self._conditioned = ""

    @property
    def raw_pending(self) -> int:
        return len(self._raw)

    @property
    def conditioned_pending(self) -> int:
        return len(self._conditioned)

    def feed(self, shots: Iterable[str]) -> list[str]:
        self._raw += "".join(shots)
        blocks = len(self._raw) // EXTRACTOR_INPUT_BITS
        for index in range(blocks):
            block = self._raw[index * EXTRACTOR_INPUT_BITS:(index + 1) * EXTRACTOR_INPUT_BITS]
            self._conditioned += condition_block(block)
        self._raw = self._raw[blocks * EXTRACTOR_INPUT_BITS:]
        usable = len(self._conditioned) // self.width * self.width
        candidates = [self._conditioned[i:i + self.width] for i in range(0, usable, self.width)]
        self._conditioned = self._conditioned[usable:]
        return candidates
