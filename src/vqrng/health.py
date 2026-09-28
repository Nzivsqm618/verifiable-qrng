"""Continuous health tests on the raw bit stream, before conditioning.

The Repetition Count Test and the Adaptive Proportion Test of NIST SP 800-90B
section 4.4, for a binary source. Their cutoffs follow from a min-entropy
H_min per raw bit and a false-positive rate alpha = 2^-20. H_min is not
measured here: it is the same assumed 0.5 bits per raw bit that the 512 -> 256
HMAC conditioning relies on. The tests catch a stuck or grossly biased source.
They cannot tell a good PRNG, or a biased but moving source, from a quantum one.

Standard library only, so Level B can replay them offline.
"""

from __future__ import annotations

import math
from itertools import groupby

H_MIN = 0.5
ALPHA_EXPONENT = 20
APT_WINDOW = 1024
NOT_ENOUGH_BITS = "not_enough_bits"


class EntropyHealthError(RuntimeError):
    """The raw bits failed a continuous health test, so they were not conditioned."""


def rct_cutoff(h_min: float) -> int:
    """Repetition Count Test cutoff: 1 + ceil(-log2(alpha) / H_min)."""
    return 1 + math.ceil(ALPHA_EXPONENT / h_min)


def apt_cutoff(h_min: float, window: int = APT_WINDOW) -> int:
    """Adaptive Proportion Test cutoff: the smallest count c with P(X >= c) <= alpha,
    for X ~ Binomial(window, 2^-H_min). Equal to 1 + CRITBINOM(window, 2^-H_min, 1 - alpha)."""
    alpha = 2.0**-ALPHA_EXPONENT
    p = 2.0**-h_min
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n = math.lgamma(window + 1)
    tail = 0.0
    for k in range(window, 0, -1):
        term = math.exp(log_n - math.lgamma(k + 1) - math.lgamma(window - k + 1) + k * log_p + (window - k) * log_q)
        if tail + term > alpha:
            return k + 1
        tail += term
    return 1


class HealthMonitor:
    """Runs both tests over a raw bit stream fed in any number of pieces.

    The result depends only on the concatenated stream, so the verifier gets
    the same summary by feeding the whole tape at once.
    """

    def __init__(self, h_min: float = H_MIN) -> None:
        self.h_min = h_min
        self.rct_cutoff = rct_cutoff(h_min)
        self.apt_cutoff = apt_cutoff(h_min)
        self.bits = 0
        self.longest_run = 0
        self.windows = 0
        self.max_count = 0
        self._last = ""
        self._run = 0
        self._window = ""

    def feed(self, bits: str) -> None:
        self.bits += len(bits)
        for value, group in groupby(bits):
            length = sum(1 for _ in group)
            self._run = self._run + length if value == self._last else length
            self._last = value
            self.longest_run = max(self.longest_run, self._run)
        self._window += bits
        while len(self._window) >= APT_WINDOW:
            window, self._window = self._window[:APT_WINDOW], self._window[APT_WINDOW:]
            self.windows += 1
            self.max_count = max(self.max_count, window.count(window[0]))

    @property
    def failure(self) -> str | None:
        """Why the stream failed, or ``None`` while both tests pass."""
        if self.longest_run >= self.rct_cutoff:
            return (
                f"repetition count test failed: {self.longest_run} identical raw bits in a row, "
                f"cutoff {self.rct_cutoff} (assumed H_min = {self.h_min:g})."
            )
        if self.max_count >= self.apt_cutoff:
            return (
                f"adaptive proportion test failed: {self.max_count} of {APT_WINDOW} raw bits in a window "
                f"equal its first bit, cutoff {self.apt_cutoff} (assumed H_min = {self.h_min:g})."
            )
        return None

    def summary(self) -> dict:
        apt: dict | str = NOT_ENOUGH_BITS
        if self.windows:
            apt = {
                "cutoff": self.apt_cutoff,
                "window": APT_WINDOW,
                "windows": self.windows,
                "max_count": self.max_count,
                "passed": self.max_count < self.apt_cutoff,
            }
        return {
            "h_min": self.h_min,
            "assumed": True,
            "alpha_exponent": ALPHA_EXPONENT,
            "bits": self.bits,
            "rct": {"cutoff": self.rct_cutoff, "longest_run": self.longest_run,
                    "passed": self.longest_run < self.rct_cutoff},
            "apt": apt,
        }
