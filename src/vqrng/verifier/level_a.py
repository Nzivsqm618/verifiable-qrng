"""Level A verification: replay stored bitstrings through rejection sampling.

Runs fully offline and depends only on the standard library, so evidence can be
audited without Qiskit, a simulator, or network access.
"""

from __future__ import annotations

from typing import Any

from vqrng.evidence import KIND_SEED, QSEED_EXPANDER, SEED_BYTES, STATUS_COMPLETED, STATUS_PARTIAL


def expected_bits(range_size: int) -> int:
    """Return ``ceil(log2(range_size))``, with a floor of one qubit.

    Uses integer arithmetic so large ranges are not subject to float rounding.
    """
    return max(1, (range_size - 1).bit_length())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_bits(bits: Any, n_bits: int) -> int | None:
    if not isinstance(bits, str) or len(bits) != n_bits or set(bits) - {"0", "1"}:
        return None
    return int(bits, 2)


def _bounds(evidence: dict) -> tuple[Any, Any]:
    range_info = evidence.get("range")
    if isinstance(range_info, dict):
        return range_info.get("min"), range_info.get("max")
    return evidence.get("min_val"), evidence.get("max_val")


def _format_options(evidence: dict) -> tuple[Any, bool]:
    request = evidence.get("request")
    source = request if isinstance(request, dict) else evidence
    return source.get("digits"), bool(source.get("pad"))


def _verify_item(
    position: int, item: Any, min_val: int, range_size: int, n_bits: int,
    digits: Any, pad: bool,
) -> list[str]:
    label = f"items[{position}]"
    if not isinstance(item, dict):
        return [f"{label}: expected an object, got {type(item).__name__}."]

    errors: list[str] = []

    if item.get("index", position) != position:
        errors.append(f"{label}: index is {item.get('index')!r}, expected {position}.")

    rejected = item.get("rejected", [])
    if not isinstance(rejected, list):
        errors.append(f"{label}: 'rejected' must be a list.")
        rejected = []
    for j, bits in enumerate(rejected):
        candidate = _parse_bits(bits, n_bits)
        if candidate is None:
            errors.append(f"{label}: rejected[{j}] {bits!r} is not a {n_bits}-bit binary string.")
        elif candidate < range_size:
            errors.append(
                f"{label}: rejected[{j}] {bits!r} decodes to {candidate}, which is within "
                f"range size {range_size} and should have been accepted."
            )

    bitstring = item.get("bitstring")
    candidate = _parse_bits(bitstring, n_bits)
    if candidate is None:
        errors.append(f"{label}: bitstring {bitstring!r} is not a {n_bits}-bit binary string.")
        return errors
    if candidate >= range_size:
        errors.append(
            f"{label}: bitstring {bitstring!r} decodes to {candidate}, which is out of "
            f"range size {range_size} and should have been rejected."
        )
        return errors

    expected_number = min_val + candidate
    number = item.get("number")
    if number != expected_number or not _is_int(number):
        errors.append(
            f"{label}: number is {number!r}, but bitstring {bitstring!r} maps to {expected_number}."
        )

    if digits is not None and pad:
        expected_formatted = str(expected_number).zfill(digits)
        if item.get("formatted") != expected_formatted:
            errors.append(
                f"{label}: formatted is {item.get('formatted')!r}, expected {expected_formatted!r}."
            )

    return errors


def verify_level_a(evidence: dict) -> tuple[bool, list[str]]:
    """Replay every item in ``evidence`` through the rejection sampler.

    Returns ``(True, [])`` when every accepted and rejected bitstring is
    consistent with the recorded range and numbers, otherwise ``(False, errors)``.
    """
    if not isinstance(evidence, dict):
        return False, [f"Evidence must be an object, got {type(evidence).__name__}."]

    min_val, max_val = _bounds(evidence)
    n_bits = evidence.get("n_bits")
    items = evidence.get("items")

    errors: list[str] = []
    if not _is_int(min_val) or not _is_int(max_val):
        errors.append(f"Range bounds must be integers, got min={min_val!r}, max={max_val!r}.")
    elif min_val > max_val:
        errors.append(f"Range min ({min_val}) is greater than max ({max_val}).")
    if not _is_int(n_bits) or n_bits < 1:
        errors.append(f"n_bits must be a positive integer, got {n_bits!r}.")
    if not isinstance(items, list) or (not items and evidence.get("status") != STATUS_PARTIAL):
        errors.append("Evidence must contain a non-empty 'items' list.")
    if errors:
        return False, errors

    range_size = max_val - min_val + 1
    range_info = evidence.get("range")
    recorded_size = range_info.get("size", range_size) if isinstance(range_info, dict) else range_size
    if recorded_size != range_size:
        errors.append(f"Range size is {recorded_size!r}, expected {range_size}.")

    required_bits = expected_bits(range_size)
    if n_bits != required_bits:
        errors.append(
            f"n_bits is {n_bits}, but range size {range_size} requires {required_bits} bits."
        )
        return False, errors

    digits, pad = _format_options(evidence)
    if pad and not (_is_int(digits) and digits >= 1):
        errors.append(f"pad is set but digits is {digits!r}.")
        digits = None

    for position, item in enumerate(items):
        errors.extend(_verify_item(position, item, min_val, range_size, n_bits, digits, pad))
    if evidence.get("kind") == KIND_SEED:
        errors.extend(_verify_seed(evidence, min_val, max_val, items))

    return not errors, errors


def _verify_seed(evidence: dict, min_val: int, max_val: int, items: list[Any]) -> list[str]:
    """A seed record is SEED_BYTES values over [0, 255]; ``seed`` is those bytes as hex."""
    if (min_val, max_val) != (0, 255):
        return [f"a seed record must use the range [0, 255], got [{min_val}, {max_val}]."]
    errors: list[str] = []
    if evidence.get("expander") != QSEED_EXPANDER:
        errors.append(f"expander is {evidence.get('expander')!r}, expected {QSEED_EXPANDER!r}.")
    seed = evidence.get("seed")
    if evidence.get("status") != STATUS_COMPLETED:
        if seed is not None:
            errors.append("seed must be null in a partial seed record.")
        return errors
    numbers = [item.get("number") if isinstance(item, dict) else None for item in items]
    if len(numbers) != SEED_BYTES or not all(_is_int(n) and 0 <= n <= 255 for n in numbers):
        return errors + [f"a completed seed record needs {SEED_BYTES} byte values, got {len(numbers)} items."]
    expected = bytes(numbers).hex()
    if seed != expected:
        errors.append(f"seed is {seed!r}, but the items give {expected!r}.")
    return errors
