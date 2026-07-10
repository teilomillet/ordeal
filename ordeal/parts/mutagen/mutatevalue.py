from __future__ import annotations
# ruff: noqa
import math
import random as _random
import struct
from typing import Any
def mutate_value(value: Any, rng: _random.Random, intensity: float = 0.3) -> Any:
    """Mutate a single Python value — the core AFL bit-flip adapted for Python types.

    Used by ``mine()`` Phase 2 (coverage-guided input mutation) and
    ``Explorer`` (seed mutation from productive checkpoints).

    The mutation is type-aware: integers get bit-flips and arithmetic
    perturbations, strings get character swaps and truncation, floats
    get mantissa perturbation and special values, etc.

    Args:
        value: The value to mutate.
        rng: Random number generator (seeded for determinism).
        intensity: Probability of each mutation firing (0.0-1.0).
            Lower = subtle perturbation, higher = aggressive mutation.

    Returns:
        A mutated copy of the value.  May return the original unchanged
        if no mutation was selected (by design — some inputs should be
        retested as-is to confirm determinism).
    """
    if isinstance(value, bool):
        return not value if rng.random() < intensity else value

    if isinstance(value, int):
        return _mutate_int(value, rng, intensity)

    if isinstance(value, float):
        return _mutate_float(value, rng, intensity)

    if isinstance(value, str):
        return _mutate_str(value, rng, intensity)

    if isinstance(value, bytes):
        return _mutate_bytes(value, rng, intensity)

    if isinstance(value, list):
        return _mutate_list(value, rng, intensity)

    if isinstance(value, dict):
        return _mutate_dict(value, rng, intensity)

    if isinstance(value, tuple):
        mutated = _mutate_list(list(value), rng, intensity)
        return tuple(mutated)

    # Unknown type — return as-is
    return value
def _mutate_int(value: int, rng: _random.Random, intensity: float) -> int:
    """Mutate an integer via bit-flips and arithmetic perturbation.

    Strategies (each fires with probability ``intensity``):
    - Flip a random bit (the core AFL mutation)
    - Add/subtract a small delta (-16 to +16)
    - Replace with a boundary value (0, -1, 2^31-1, etc.)
    """
    result = value

    # Bit flip — the fundamental mutation
    if rng.random() < intensity:
        bit = rng.randint(0, 63)
        result ^= 1 << bit

    # Arithmetic perturbation — explore nearby values
    if rng.random() < intensity:
        delta = rng.randint(-16, 16)
        result += delta

    # Boundary values — where bugs cluster
    if rng.random() < intensity * 0.3:
        result = rng.choice([0, -1, 1, 2**7 - 1, 2**15 - 1, 2**31 - 1, -(2**31)])

    return result
def _mutate_float(value: float, rng: _random.Random, intensity: float) -> float:
    """Mutate a float via mantissa perturbation and special values.

    Strategies:
    - Perturb mantissa bits (small numerical change)
    - Negate
    - Replace with special values (0.0, NaN, Inf, -Inf, epsilon)
    """
    result = value

    # Mantissa bit-flip — the float equivalent of AFL's bit-flip
    if rng.random() < intensity and math.isfinite(value):
        try:
            raw = struct.pack("d", value)
            byte_list = bytearray(raw)
            bit_idx = rng.randint(0, 51)  # mantissa is bits 0-51
            byte_pos = bit_idx // 8
            bit_pos = bit_idx % 8
            byte_list[byte_pos] ^= 1 << bit_pos
            (result,) = struct.unpack("d", bytes(byte_list))
        except (struct.error, OverflowError):
            pass

    # Negate
    if rng.random() < intensity * 0.5:
        result = -result

    # Special values — where floating-point bugs hide
    if rng.random() < intensity * 0.3:
        result = rng.choice(
            [0.0, -0.0, 1.0, -1.0, float("nan"), float("inf"), float("-inf"), 1e-300, 1e300]
        )

    return result
def _mutate_str(value: str, rng: _random.Random, intensity: float) -> str:
    """Mutate a string via character substitution, insertion, deletion.

    Strategies:
    - Flip a character to a nearby ASCII value
    - Insert a random character
    - Delete a character
    - Replace with boundary strings (empty, null byte, long)
    """
    if not value:
        # Empty string — generate a short one
        if rng.random() < intensity:
            return chr(rng.randint(32, 126))
        return value

    chars = list(value)

    # Character flip — the string equivalent of bit-flip
    if rng.random() < intensity and chars:
        idx = rng.randint(0, len(chars) - 1)
        delta = rng.randint(-3, 3)
        new_ord = max(0, min(0x10FFFF, ord(chars[idx]) + delta))
        chars[idx] = chr(new_ord)

    # Insert
    if rng.random() < intensity * 0.5:
        idx = rng.randint(0, len(chars))
        chars.insert(idx, chr(rng.randint(32, 126)))

    # Delete
    if rng.random() < intensity * 0.5 and len(chars) > 1:
        idx = rng.randint(0, len(chars) - 1)
        chars.pop(idx)

    # Boundary strings
    if rng.random() < intensity * 0.2:
        return rng.choice(["", "\x00", "A" * 1000, " ", "\n", "\t"])

    return "".join(chars)
def _mutate_bytes(value: bytes, rng: _random.Random, intensity: float) -> bytes:
    """Mutate bytes via the exact AFL bit-flip pattern."""
    if not value:
        return b"\x00" if rng.random() < intensity else value

    data = bytearray(value)
    for i in range(len(data)):
        for j in range(8):
            if rng.random() < intensity:
                data[i] ^= 1 << j
    return bytes(data)
def _mutate_list(value: list, rng: _random.Random, intensity: float) -> list:
    """Mutate a list by mutating elements, inserting, or deleting."""
    result = list(value)

    # Mutate existing elements
    for i in range(len(result)):
        if rng.random() < intensity:
            result[i] = mutate_value(result[i], rng, intensity)

    # Insert
    if rng.random() < intensity * 0.3 and result:
        idx = rng.randint(0, len(result))
        # Clone and mutate an existing element
        donor = result[rng.randint(0, len(result) - 1)]
        result.insert(idx, mutate_value(donor, rng, intensity))

    # Delete
    if rng.random() < intensity * 0.3 and len(result) > 1:
        result.pop(rng.randint(0, len(result) - 1))

    return result
def _mutate_dict(value: dict, rng: _random.Random, intensity: float) -> dict:
    """Mutate a dict by mutating values (keys are preserved)."""
    result = dict(value)
    for key in list(result):
        if rng.random() < intensity:
            result[key] = mutate_value(result[key], rng, intensity)
    return result
def _lazy_strategy_parts(strategy: Any) -> tuple[str | None, tuple[Any, ...], dict[str, Any]]:
    """Extract Hypothesis LazyStrategy metadata without forcing evaluation."""
    fn = getattr(strategy, "function", None)
    name = getattr(fn, "__name__", None)
    args = tuple(getattr(strategy, "_LazyStrategy__args", ()))
    kwargs = dict(getattr(strategy, "_LazyStrategy__kwargs", {}))
    return name, args, kwargs
def extract_strategy_constraint(strategy: Any) -> dict[str, Any] | None:
    """Return a lightweight mutation constraint for a Hypothesis strategy.

    The returned constraint is intentionally small and only covers the
    common strategies that Explorer seed mutation needs to keep values
    "nearby but still valid": integers, floats, text, lists, booleans,
    and sampled/just strategies.
    """
    elements = getattr(strategy, "elements", None)
    if elements is not None:
        return {"kind": "choices", "choices": tuple(elements)}

    name, args, kwargs = _lazy_strategy_parts(strategy)
    if name == "integers":
        min_value = args[0] if len(args) > 0 else None
        max_value = args[1] if len(args) > 1 else None
        return {"kind": "int", "min": min_value, "max": max_value}
    if name == "floats":
        min_value = args[0] if len(args) > 0 else None
        max_value = args[1] if len(args) > 1 else None
        return {
            "kind": "float",
            "min": min_value,
            "max": max_value,
            "allow_nan": kwargs.get("allow_nan", True),
            "allow_infinity": kwargs.get("allow_infinity", True),
        }
    if name == "text":
        return {
            "kind": "text",
            "min_size": kwargs.get("min_size", 0),
            "max_size": kwargs.get("max_size"),
        }
    if name == "binary":
        return {
            "kind": "bytes",
            "min_size": kwargs.get("min_size", 0),
            "max_size": kwargs.get("max_size"),
        }
    if name == "lists":
        element = extract_strategy_constraint(args[0]) if args else None
        return {
            "kind": "list",
            "min_size": kwargs.get("min_size", 0),
            "max_size": kwargs.get("max_size"),
            "element": element,
        }
    if name == "tuples":
        return {
            "kind": "tuple",
            "items": tuple(extract_strategy_constraint(arg) for arg in args),
        }
    if name == "fixed_dictionaries":
        required = args[0] if args and isinstance(args[0], dict) else {}
        optional = kwargs.get("optional") if isinstance(kwargs.get("optional"), dict) else {}
        return {
            "kind": "dict",
            "required": {
                key: constraint
                for key, strategy in required.items()
                if (constraint := extract_strategy_constraint(strategy)) is not None
            },
            "optional": {
                key: constraint
                for key, strategy in optional.items()
                if (constraint := extract_strategy_constraint(strategy)) is not None
            },
        }
    if name == "booleans":
        return {"kind": "bool"}
    if name == "just" and args:
        return {"kind": "choices", "choices": (args[0],)}
    return None
def _default_for_constraint(constraint: dict[str, Any], rng: _random.Random) -> Any:
    """Construct a simple valid value when repair has no usable baseline."""
    kind = constraint.get("kind")
    if kind == "choices":
        choices = tuple(constraint.get("choices", ()))
        return rng.choice(choices) if choices else None
    if kind == "bool":
        return False
    if kind == "int":
        min_value = constraint.get("min")
        max_value = constraint.get("max")
        if min_value is not None and max_value is not None:
            return max(int(min_value), min(int(max_value), 0))
        if min_value is not None:
            return int(min_value)
        if max_value is not None:
            return int(max_value)
        return 0
    if kind == "float":
        min_value = constraint.get("min")
        max_value = constraint.get("max")
        if min_value is not None and max_value is not None:
            return max(float(min_value), min(float(max_value), 0.0))
        if min_value is not None:
            return float(min_value)
        if max_value is not None:
            return float(max_value)
        return 0.0
    if kind == "text":
        return "x" * int(constraint.get("min_size", 0))
    if kind == "bytes":
        return b"\x00" * int(constraint.get("min_size", 0))
    if kind == "list":
        return []
    if kind == "tuple":
        items = tuple(constraint.get("items", ()))
        return tuple(
            _default_for_constraint(item, rng) if item is not None else None for item in items
        )
    if kind == "dict":
        required = dict(constraint.get("required", {}))
        return {key: _default_for_constraint(item, rng) for key, item in required.items()}
    return None
