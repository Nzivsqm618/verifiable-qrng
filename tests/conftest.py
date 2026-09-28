"""Shared fixtures, and the path-coverage plugin when tests/path_coverage is present."""

from __future__ import annotations

import pytest

from vqrng import extractor


@pytest.fixture
def passthrough_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Condition every raw bit to itself, so a test can choose the sampled candidates.

    Only the HMAC step is replaced; the conditioned stream, its replay in
    Level B, and rejection sampling run as usual. tests/test_extractor.py
    covers the real conditioning end to end.
    """
    monkeypatch.setattr(extractor, "EXTRACTOR_INPUT_BITS", 1)
    monkeypatch.setattr(extractor, "EXTRACTOR_OUTPUT_BITS", 1)
    monkeypatch.setattr(extractor, "condition_block", lambda block: block)


def pytest_addoption(parser: object) -> None:
    addoption = getattr(parser, "addoption")
    addoption(
        "--path-cov",
        action="store_true",
        help="Report basis path coverage for vqrng. Requires tests/path_coverage. Run with --no-cov.",
    )


def pytest_configure(config: object) -> None:
    getoption = getattr(config, "getoption")
    if not getoption("--path-cov"):
        return
    from tests.path_coverage.plugin import PathCoverage

    pluginmanager = getattr(config, "pluginmanager")
    pluginmanager.register(PathCoverage(), "vqrng-path-coverage")
