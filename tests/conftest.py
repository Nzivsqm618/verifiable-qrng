"""Load the path-coverage plugin when tests/path_coverage is present."""

from __future__ import annotations


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
