"""Value-level mutation — AFL's bit-flip loop for Python values.

Real fuzzers don't generate inputs from scratch.  They start from a
known-good input that reached interesting coverage, then **mutate** it:
flip a bit, swap a byte, nudge a value.  If the mutation reaches NEW
coverage, it becomes a new seed for further mutation.  This is the core
loop that makes AFL and libFuzzer scale with compute.

ordeal's Hypothesis integration generates from type-level strategies
(``st.integers()``, ``st.text()``).  This module adds the complementary
approach: take a **concrete Python value** and perturb it.

The mutation is type-aware at the Python level::

    mutate_value(42, rng)          → 43, 41, 0, -42, 2**31
    mutate_value("admin", rng)     → "bdmin", "admiN", "", "admin\\x00"
    mutate_value(3.14, rng)        → 3.15, -3.14, 0.0, float('nan'), float('inf')
    mutate_value([1, 2, 3], rng)   → [1, 3], [1, 2, 3, 0], [1, 99, 3]
    mutate_value(True, rng)        → False

Combined with coverage feedback, this is the closed loop::

    input → fn() → coverage → mutation → new input → fn() → more coverage
                                  ↑                              ↓
                            keep productive                 discard if
                              mutations                    no new edges

The ``mutate_inputs`` function takes a full kwargs dict (like those
in ``MineResult.collected_inputs``) and returns a mutated copy.  Wire
this into mine()'s collection loop to explore near known-good inputs
instead of generating blind random ones.

Scales with compute: each mutation is cheap (O(1) per value).  More
CPU time = more mutations = more coverage, as long as the feedback
loop prunes unproductive ones.
"""

from __future__ import annotations

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


def _repair_to_constraint(
    value: Any,
    original: Any,
    constraint: dict[str, Any],
    rng: _random.Random,
) -> Any:
    """Project a mutated value back into the declared strategy envelope."""
    kind = constraint.get("kind")

    if kind == "choices":
        choices = tuple(constraint.get("choices", ()))
        if value in choices:
            return value
        if original in choices:
            return original
        return rng.choice(choices) if choices else original

    if kind == "bool":
        return bool(value)

    if kind == "int":
        candidate = value if isinstance(value, int) and not isinstance(value, bool) else original
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            candidate = _default_for_constraint(constraint, rng)
        min_value = constraint.get("min")
        max_value = constraint.get("max")
        if min_value is not None:
            candidate = max(candidate, int(min_value))
        if max_value is not None:
            candidate = min(candidate, int(max_value))
        return candidate

    if kind == "float":
        candidate = (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else original
        )
        if not isinstance(candidate, (int, float)) or isinstance(candidate, bool):
            candidate = _default_for_constraint(constraint, rng)
        candidate = float(candidate)
        if math.isnan(candidate) and not constraint.get("allow_nan", True):
            candidate = float(_default_for_constraint(constraint, rng))
        if math.isinf(candidate) and not constraint.get("allow_infinity", True):
            candidate = float(_default_for_constraint(constraint, rng))
        min_value = constraint.get("min")
        max_value = constraint.get("max")
        if min_value is not None and math.isfinite(candidate):
            candidate = max(candidate, float(min_value))
        if max_value is not None and math.isfinite(candidate):
            candidate = min(candidate, float(max_value))
        return candidate

    if kind == "text":
        candidate = (
            value
            if isinstance(value, str)
            else (
                original
                if isinstance(original, str)
                else _default_for_constraint(constraint, rng)
            )
        )
        min_size = int(constraint.get("min_size", 0))
        max_size = constraint.get("max_size")
        if max_size is not None and len(candidate) > int(max_size):
            candidate = candidate[: int(max_size)]
        if len(candidate) < min_size:
            pad_char = original[0] if isinstance(original, str) and original else "x"
            candidate = candidate + pad_char * (min_size - len(candidate))
        return candidate

    if kind == "bytes":
        candidate = (
            value
            if isinstance(value, bytes)
            else (
                original
                if isinstance(original, bytes)
                else _default_for_constraint(constraint, rng)
            )
        )
        min_size = int(constraint.get("min_size", 0))
        max_size = constraint.get("max_size")
        if max_size is not None and len(candidate) > int(max_size):
            candidate = candidate[: int(max_size)]
        if len(candidate) < min_size:
            candidate = candidate + (b"\x00" * (min_size - len(candidate)))
        return candidate

    if kind == "list":
        if isinstance(value, list):
            candidate = list(value)
        elif isinstance(original, list):
            candidate = list(original)
        else:
            candidate = []
        min_size = int(constraint.get("min_size", 0))
        max_size = constraint.get("max_size")
        element_constraint = constraint.get("element")
        if element_constraint is not None:
            repaired: list[Any] = []
            originals = list(original) if isinstance(original, list) else []
            for idx, item in enumerate(candidate):
                baseline = originals[idx] if idx < len(originals) else item
                repaired.append(_repair_to_constraint(item, baseline, element_constraint, rng))
            candidate = repaired
        if max_size is not None and len(candidate) > int(max_size):
            candidate = candidate[: int(max_size)]
        if len(candidate) < min_size:
            originals = list(original) if isinstance(original, list) else []
            while len(candidate) < min_size:
                if len(candidate) < len(originals):
                    baseline = originals[len(candidate)]
                elif originals:
                    baseline = originals[-1]
                else:
                    baseline = (
                        _default_for_constraint(element_constraint, rng)
                        if element_constraint
                        else None
                    )
                if element_constraint is not None:
                    candidate.append(
                        _repair_to_constraint(
                            baseline,
                            baseline,
                            element_constraint,
                            rng,
                        )
                    )
                else:
                    candidate.append(baseline)
        return candidate

    if kind == "tuple":
        item_constraints = tuple(constraint.get("items", ()))
        candidate_values = tuple(value) if isinstance(value, (tuple, list)) else ()
        original_values = tuple(original) if isinstance(original, tuple) else ()
        repaired_items: list[Any] = []
        for idx, item_constraint in enumerate(item_constraints):
            baseline = (
                candidate_values[idx]
                if idx < len(candidate_values)
                else (
                    original_values[idx]
                    if idx < len(original_values)
                    else (
                        _default_for_constraint(item_constraint, rng)
                        if item_constraint is not None
                        else None
                    )
                )
            )
            if item_constraint is None:
                repaired_items.append(baseline)
            else:
                original_item = original_values[idx] if idx < len(original_values) else baseline
                repaired_items.append(
                    _repair_to_constraint(baseline, original_item, item_constraint, rng)
                )
        return tuple(repaired_items)

    if kind == "dict":
        candidate = dict(value) if isinstance(value, dict) else {}
        original_dict = dict(original) if isinstance(original, dict) else {}
        repaired: dict[str, Any] = {}
        required = dict(constraint.get("required", {}))
        optional = dict(constraint.get("optional", {}))

        for key, subconstraint in required.items():
            baseline = candidate.get(key, original_dict.get(key))
            if baseline is None:
                baseline = _default_for_constraint(subconstraint, rng)
            repaired[key] = _repair_to_constraint(
                baseline,
                original_dict.get(key, baseline),
                subconstraint,
                rng,
            )

        for key, subconstraint in optional.items():
            if key not in candidate and key not in original_dict:
                continue
            baseline = candidate.get(key, original_dict.get(key))
            repaired[key] = _repair_to_constraint(
                baseline,
                original_dict.get(key, baseline),
                subconstraint,
                rng,
            )

        for key, item in candidate.items():
            if key not in repaired:
                repaired[key] = item
        return repaired

    return value


def mutate_inputs(
    inputs: dict[str, Any],
    rng: _random.Random,
    intensity: float = 0.3,
    *,
    strategies: dict[str, Any] | None = None,
    respect_strategies: bool | None = None,
    constraints: dict[str, dict[str, Any]] | None = None,
    stay_within_bounds: bool = False,
) -> dict[str, Any]:
    """Mutate a full kwargs dict — used by mine() and Explorer's seed mutation loop.

    Takes a known-good input that reached interesting coverage and
    perturbs it.  The coverage feedback loop then checks if the
    mutation reaches new edges::

        for good_input in productive_inputs:
            mutated = mutate_inputs(good_input, rng)
            edges_before = collector.snapshot()
            fn(**mutated)
            edges_after = collector.snapshot()
            if edges_after - edges_before:
                # This mutation found new coverage — keep it as a seed
                productive_inputs.append(mutated)

    Args:
        inputs: Function kwargs to mutate (e.g. ``{"x": 42, "mode": "admin"}``).
        rng: Seeded RNG for deterministic mutation.
        intensity: Mutation aggressiveness (0.0-1.0).
        strategies: Optional Hypothesis strategies keyed by parameter name.
            When provided, common bounds are extracted automatically.
        respect_strategies: Backward-compatible alias for
            ``stay_within_bounds`` when callers are thinking in terms of
            declared strategies rather than explicit constraints.
        constraints: Optional per-parameter strategy constraints extracted
            from Hypothesis strategies.
        stay_within_bounds: If ``True``, project each mutated value back into
            its declared strategy bounds.  Useful for config/control-plane
            systems where "nearby but still valid" beats boundary-breaking
            mutations.

    Returns:
        A new dict with mutated values.  Keys are preserved.
    """
    if respect_strategies is not None:
        stay_within_bounds = respect_strategies
    if constraints is None and strategies is not None:
        constraints = {
            key: constraint
            for key, strategy in strategies.items()
            if (constraint := extract_strategy_constraint(strategy)) is not None
        }

    mutated = {key: mutate_value(val, rng, intensity) for key, val in inputs.items()}
    if not stay_within_bounds or not constraints:
        return mutated

    repaired: dict[str, Any] = {}
    for key, original in inputs.items():
        value = mutated.get(key, original)
        constraint = constraints.get(key)
        repaired[key] = (
            _repair_to_constraint(value, original, constraint, rng) if constraint else value
        )
    return repaired
