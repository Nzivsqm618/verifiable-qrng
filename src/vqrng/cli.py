"""Command-line entrypoint: ``vqrng [OPTIONS] [MIN_VAL] [MAX_VAL]``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence

import vqrng

EXIT_OK = 0
EXIT_ERROR = 1
_RUNTIME_LOG_LEVEL = "QISKIT_IBM_RUNTIME_LOG_LEVEL"


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="vqrng",
        description="Generate verifiable quantum random integers.",
        add_help=False,
    )
    parser.add_argument("min_val", metavar="MIN_VAL", type=int, nargs="?", help="Inclusive lower bound.")
    parser.add_argument("max_val", metavar="MAX_VAL", type=int, nargs="?", help="Inclusive upper bound.")
    parser.add_argument("-d", "--digits", type=_positive_int, metavar="INTEGER",
                        help="Generate N-digit numbers instead of passing MIN_VAL MAX_VAL.")
    parser.add_argument("--pad", action="store_true",
                        help="With --digits, allow leading zeros and print zero-padded strings.")

    output = parser.add_mutually_exclusive_group()
    output.add_argument("-j", "--json", action="store_true", help="Print the full JSON evidence payload.")
    output.add_argument("-r", "--raw", action="store_true", help="Print numbers space-separated on one line.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-s", "--simulator", dest="mode", action="store_const", const="aer",
                      help="Run on the local Qiskit Aer simulator (default).")
    mode.add_argument("-h", "--hardware", dest="mode", action="store_const", const="hardware",
                      help="Run on IBM Quantum hardware (requires --runtime).")
    parser.set_defaults(mode="aer")

    parser.add_argument("-p", "--pool", type=_positive_int, default=1, metavar="INTEGER",
                        help="Number of values to generate (default: 1).")
    parser.add_argument("-t", "--runtime", type=_positive_int, metavar="INTEGER",
                        help="Maximum QPU runtime budget in seconds.")

    parser.add_argument("--help", action="help", help="Show this message and exit.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {vqrng.__version__}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)

    has_bounds = args.min_val is not None or args.max_val is not None
    if args.digits is not None and has_bounds:
        raise UsageError("use either MIN_VAL MAX_VAL or -d/--digits, not both")
    if args.digits is None:
        if not has_bounds:
            raise UsageError("provide MIN_VAL MAX_VAL or -d/--digits")
        if args.max_val is None:
            raise UsageError("MAX_VAL is required when MIN_VAL is given")
        if args.min_val > args.max_val:
            raise UsageError(f"MIN_VAL ({args.min_val}) must be <= MAX_VAL ({args.max_val})")
    if args.pad and args.digits is None:
        raise UsageError("--pad requires -d/--digits")
    if args.mode == "hardware" and args.runtime is None:
        raise UsageError("-t/--runtime is required with -h/--hardware")
    return args


def render(evidence: dict, as_json: bool, raw: bool) -> str:
    if as_json:
        return json.dumps(evidence, indent=2)
    values = [item["formatted"] for item in evidence["items"]]
    return (" " if raw else "\n").join(values)


def _configure_stdio() -> None:
    """Write each stream straight through so shutdown cannot flush it a second time."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True, write_through=True)
        except (OSError, ValueError):
            pass


def _write(stream: object, text: str) -> None:
    write = getattr(stream, "write")
    write(text if text.endswith("\n") else text + "\n")
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def _quiet_runtime_logs() -> tuple[str | None, int]:
    """Keep IBM Runtime warnings off the console unless the user opted into them.

    Those warnings share the terminal with stdout. On Windows the console
    replays that mixed buffer, so the same numbers show up twice.
    """
    logger = logging.getLogger("qiskit_ibm_runtime")
    previous_level = logger.level
    previous_env = os.environ.get(_RUNTIME_LOG_LEVEL)
    if previous_env is None:
        os.environ[_RUNTIME_LOG_LEVEL] = "ERROR"
        logger.setLevel(logging.ERROR)
    return previous_env, previous_level


def _restore_runtime_logs(previous_env: str | None, previous_level: int) -> None:
    logger = logging.getLogger("qiskit_ibm_runtime")
    if previous_env is None:
        os.environ.pop(_RUNTIME_LOG_LEVEL, None)
        logger.setLevel(previous_level)
    else:
        os.environ[_RUNTIME_LOG_LEVEL] = previous_env


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    try:
        args = parse_args(argv)
    except UsageError as exc:
        _write(sys.stderr, f"vqrng: error: {exc}")
        _write(sys.stderr, "Try 'vqrng --help' for usage.")
        return EXIT_ERROR

    _write(sys.stderr, f"vqrng: generating {args.pool} value(s) on {args.mode}")
    previous_env, previous_level = _quiet_runtime_logs()
    try:
        try:
            evidence = vqrng.generate(
                min_val=args.min_val,
                max_val=args.max_val,
                digits=args.digits,
                pad=args.pad,
                mode=args.mode,
                pool_size=args.pool,
                runtime_limit=args.runtime,
            )
        except Exception as exc:
            _write(sys.stderr, f"vqrng: error: {exc}")
            return EXIT_ERROR
        sys.stderr.flush()
        _write(sys.stdout, render(evidence, args.json, args.raw))
        return EXIT_OK
    finally:
        _restore_runtime_logs(previous_env, previous_level)


if __name__ == "__main__":
    sys.exit(main())
