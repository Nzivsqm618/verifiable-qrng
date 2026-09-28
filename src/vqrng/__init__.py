"""Verifiable Quantum Random Number Generator (vqrng)."""

from vqrng.backends import AerBackend, BackendRun, BaseBackend, IBMBackend
from vqrng.core import generate, resolve_range
from vqrng.verifier import VerificationResult, verify

__version__ = "0.1.0"
__all__ = [
    "AerBackend",
    "BackendRun",
    "BaseBackend",
    "IBMBackend",
    "generate",
    "resolve_range",
    "verify",
    "VerificationResult",
    "__version__",
]
