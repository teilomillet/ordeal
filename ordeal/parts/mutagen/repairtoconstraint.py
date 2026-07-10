from __future__ import annotations
# ruff: noqa
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
            value if isinstance(value, (int, float)) and not isinstance(value, bool) else original
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
                original if isinstance(original, str) else _default_for_constraint(constraint, rng)
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
