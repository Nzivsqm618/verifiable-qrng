"""Verifiable Quantum Random Number Generator (vqrng)."""

from vqrng.backends import AerBackend, BackendJobError, BackendRun, BaseBackend, IBMBackend
from vqrng.core import GenerationError, collect_seed, generate, resolve_range
from vqrng.extractor import extract_entropy
from vqrng.health import EntropyHealthError
from vqrng.qseed import QSeed, QSeedError
from vqrng.verifier import LevelResult, VerificationResult, verify

__version__ = "0.4.0"
__all__ = [
    "AerBackend",
    "BackendJobError",
    "BackendRun",
    "BaseBackend",
    "EntropyHealthError",
    "GenerationError",
    "IBMBackend",
    "LevelResult",
    "QSeed",
    "QSeedError",
    "collect_seed",
    "extract_entropy",
    "generate",
    "resolve_range",
    "verify",
    "VerificationResult",
    "__version__",
]
