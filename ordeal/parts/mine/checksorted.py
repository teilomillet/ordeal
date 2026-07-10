from __future__ import annotations
# ruff: noqa
def _check_sorted(outputs: list[Any]) -> MinedProperty:
    """Check if the output is always sorted (for list returns)."""
    total = 0
    holds = 0
    ce = None
    for i, o in enumerate(outputs):
        if not isinstance(o, (list, tuple)):
            continue
        if len(o) <= 1:
            total += 1
            holds += 1
            continue
        total += 1
        try:
            if list(o) == sorted(o):
                holds += 1
            elif ce is None:
                ce = {"index": i, "output": o}
        except TypeError:
            # uncomparable elements — skip this sample
            total -= 1
    if total == 0:
        return MinedProperty("output is sorted", 0, 0)
    return MinedProperty("output is sorted", holds, total, ce)
def _check_constant_output(outputs: list[Any]) -> MinedProperty:
    """Check if the function always returns the same value regardless of input."""
    if len(outputs) < 2:
        return MinedProperty("constant output", 0, 0)
    first = outputs[0]
    holds = sum(1 for o in outputs if _approx_equal(o, first))
    ce = None
    if holds < len(outputs):
        for i, output in enumerate(outputs[1:], start=1):
            if not _approx_equal(output, first):
                ce = {"index": i, "expected": first, "got": output}
                break
    return MinedProperty("constant output", holds, len(outputs), ce)
def _check_linear_relationship(
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> list[MinedProperty]:
    """Check if output = a*input + b holds for numeric single-param functions.

    Fits a linear model from two data points and checks whether it
    predicts the remaining outputs within tolerance.
    """
    if len(inputs) < 3 or len(outputs) < 3:
        return []

    results: list[MinedProperty] = []
    for param_name in inputs[0]:
        pairs: list[tuple[float, float]] = []
        for inp, out in zip(inputs, outputs):
            v = inp[param_name]
            if (
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and isinstance(out, (int, float))
                and not isinstance(out, bool)
                and math.isfinite(v)
                and math.isfinite(out)
            ):
                pairs.append((float(v), float(out)))

        if len(pairs) < 3:
            continue

        # Pick two distinct points to fit y = a*x + b
        x0, y0 = pairs[0]
        x1, y1 = None, None
        for x, y in pairs[1:]:
            if not _approx_equal(x, x0):
                x1, y1 = x, y
                break
        if x1 is None:
            continue

        a = (y1 - y0) / (x1 - x0)
        b = y0 - a * x0

        # Check prediction on all points
        total = len(pairs)
        holds = 0
        for x, y in pairs:
            predicted = a * x + b
            if _approx_equal(predicted, y):
                holds += 1

        if holds == total:

            def _fmt(v: float) -> str:
                if v == int(v):
                    return str(int(v))
                return f"{v:.4g}"

            results.append(
                MinedProperty(
                    f"linear: output = {_fmt(a)}*{param_name} + {_fmt(b)}",
                    holds,
                    total,
                )
            )
    return results
def _check_output_length_constant(outputs: list[Any]) -> MinedProperty:
    """Check if len(output) is always the same regardless of input."""
    lengths: list[int] = []
    for o in outputs:
        try:
            lengths.append(len(o))
        except TypeError:
            pass
    if len(lengths) < 2:
        return MinedProperty("output length constant", 0, 0)
    first = lengths[0]
    holds = sum(1 for ln in lengths if ln == first)
    return MinedProperty(f"output length always {first}", holds, len(lengths))
def _check_bijective(
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> MinedProperty:
    """Check if each unique input produces a unique output (no collisions).

    Only considers inputs/outputs that are hashable.
    """
    if len(inputs) < 2 or len(outputs) < 2:
        return MinedProperty("bijective", 0, 0)

    # Build (input_tuple, output) pairs, filtering unhashable values
    seen_inputs: dict[Any, Any] = {}
    total = 0
    for inp, out in zip(inputs, outputs):
        try:
            key = tuple(sorted(inp.items()))
            hash(key)
            hash(out)
        except TypeError:
            continue
        total += 1
        if key in seen_inputs:
            # Same input seen before — skip (determinism, not bijectivity)
            if _approx_equal(seen_inputs[key], out):
                continue
            total -= 1
            continue
        seen_inputs[key] = out

    if total < 2:
        return MinedProperty("bijective", 0, 0)

    # Check for output collisions among distinct inputs
    try:
        unique_outputs = len({v for v in seen_inputs.values()})
    except TypeError:
        return MinedProperty("bijective", 0, 0)

    unique_inputs = len(seen_inputs)
    if unique_outputs == unique_inputs:
        return MinedProperty("bijective", unique_inputs, unique_inputs)

    collisions: dict[Any, list[Any]] = {}
    for key, value in seen_inputs.items():
        collisions.setdefault(value, []).append(key)
    ce = None
    for value, keys in collisions.items():
        if len(keys) > 1:
            ce = {"output": value, "colliding_inputs": keys[:2]}
            break
    return MinedProperty("bijective", unique_outputs, unique_inputs, ce)
def _check_preserves_type(
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> MinedProperty:
    """Check if the output type always matches the type of the first input parameter."""
    if not inputs or not outputs:
        return MinedProperty("preserves type", 0, 0)

    # Use the first parameter
    param_names = list(inputs[0].keys())
    if not param_names:
        return MinedProperty("preserves type", 0, 0)

    first_param = param_names[0]
    total = 0
    holds = 0
    for inp, out in zip(inputs, outputs):
        v = inp[first_param]
        if v is None or out is None:
            continue
        total += 1
        if type(v) is type(out):
            holds += 1
    if total == 0:
        return MinedProperty("preserves type", 0, 0)
    return MinedProperty("preserves type", holds, total)
def _check_null_on_null(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """Check if passing None for any parameter returns None.

    Tests the common defensive pattern where null inputs produce null output.
    """
    if not inputs:
        return MinedProperty("None in -> None out", 0, 0)

    param_names = list(inputs[0].keys())
    if not param_names:
        return MinedProperty("None in -> None out", 0, 0)

    total = 0
    holds = 0
    for param_name in param_names:
        # Build kwargs with one param set to None, others from first sample
        base_kwargs = dict(inputs[0])
        base_kwargs[param_name] = None
        try:
            result = _call_sync(fn, **base_kwargs)
            total += 1
            if result is None:
                holds += 1
        except (TypeError, ValueError, AttributeError):
            # Function doesn't accept None — that's fine, skip
            pass

    if total == 0:
        return MinedProperty("None in -> None out", 0, 0)
    return MinedProperty("None in -> None out", holds, total)
# ============================================================================
# Main entry point
# ============================================================================


def mine(
    fn: Callable[..., Any],
    *,
    max_examples: int = 500,
    ignore_properties: list[str] | tuple[str, ...] = (),
    minimize_findings: bool = False,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> MineResult:
    """Discover likely properties of a function by running it many times.

    Simple::

        result = mine(my_function)
        for p in result.universal:
            print(p)  # properties that always held

    With fixtures::

        result = mine(my_function, model=mock_model)

    Args:
        fn: The function to mine properties from.
        max_examples: Number of random inputs to try.
        ignore_properties: Property names to suppress from the result.
        minimize_findings: Shrink and replay suspicious witnesses for durable handoff.
        **fixtures: Strategy overrides or plain values.
    """
    # Unwrap decorated functions (@ray.remote, @functools.wraps, etc.)
    # so inspect.getsource, signature, and type hints all work.
    from ordeal.auto import _unwrap

    fn = _unwrap(fn)

    # Normalize fixtures
    normalized: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized = {}
        for k, v in fixtures.items():
            if isinstance(v, st.SearchStrategy):
                normalized[k] = v
            else:
                normalized[k] = st.just(v)

    strategies = _infer_strategies(fn, normalized)
    if strategies is None:
        fname = getattr(fn, "__name__", str(fn))
        raise ValueError(
            f"Cannot infer strategies for {fname}. Provide fixtures for untyped parameters."
        )

    # CMPLOG: extract comparison values from the function's AST and inject
    # them into strategies.  This cracks guarded branches like `if x == 42`
    # that random testing will never reach.  Each extracted value is a
    # "branch point" — a fork in the state space we can spot epistemically
    # by reading the code, then systematically explore both sides.
    branch_points: dict[str, list[Any]] = {}
    try:
        from ordeal.cmplog import enhance_strategies, extract_comparison_values

        branch_points = extract_comparison_values(fn)
        strategies = enhance_strategies(strategies, fn)
    except Exception:
        pass  # CMPLOG is best-effort; fall back to blind strategies

    # Collect outputs and inputs with coverage tracking.
    # The CoverageCollector detects when new code paths are reached,
    # so we can report saturation (more examples won't help).
    outputs: list[Any] = []
    inputs: list[dict[str, Any]] = []
    edges_seen: set[int] = set()
    new_edge_count: int = 0
    stale_count: int = 0

    # Resolve target module for coverage tracking
    fn_module = getattr(fn, "__module__", "")
    target_path = fn_module if fn_module else ""

    collector = None
    if target_path:
        try:
            from ordeal.explore import CoverageCollector

            collector = CoverageCollector([target_path])
        except Exception:
            pass

    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None, derandomize=True)
        def collect(**kwargs: Any) -> None:
            nonlocal new_edge_count, stale_count
            if collector:
                collector.start()
            try:
                result = _call_sync(fn, **kwargs)
            finally:
                if collector:
                    edges = collector.stop()
                    new = edges - edges_seen
                    if new:
                        edges_seen.update(new)
                        new_edge_count += 1
                        stale_count = 0
                        # Close the feedback loop: tell Hypothesis this input
                        # was valuable. Hypothesis steers generation toward
                        # inputs that maximize this value → more new edges.
                        try:
                            from hypothesis import target as _ht

                            _ht(float(len(new)), label="new_edges")
                        except Exception:
                            pass
                    else:
                        stale_count += 1
            outputs.append(result)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass  # some inputs may crash — that's fine, we analyze what we got

    is_saturated_early = len(edges_seen) > 0 and stale_count > max(len(outputs) // 2, 10)

    # Phase 2: Mutation-based exploration.
    # Take inputs that discovered new edges and mutate them — the AFL loop.
    # This finds coverage near known-good inputs that Hypothesis's type-level
    # strategies miss.  Each mutation is cheap (O(1) per value), and the
    # coverage feedback loop prunes unproductive ones.
    if collector and inputs and not is_saturated_early:
        try:
            from ordeal.mutagen import mutate_inputs

            mutation_rng = _random.Random(42)
            # Collect productive inputs (those that found new edges)
            productive = inputs[: max(1, new_edge_count)]
            mutation_budget = max_examples // 4  # spend 25% of budget on mutations
            for _ in range(mutation_budget):
                seed_input = productive[mutation_rng.randint(0, len(productive) - 1)]
                mutated = mutate_inputs(seed_input, mutation_rng)
                collector.start()
                try:
                    result = _call_sync(fn, **mutated)
                except Exception:
                    collector.stop()
                    continue
                edges = collector.stop()
                new = edges - edges_seen
                if new:
                    edges_seen.update(new)
                    new_edge_count += 1
                    productive.append(mutated)
                outputs.append(result)
                inputs.append(mutated)
        except Exception:
            pass  # mutation phase is best-effort

    is_saturated_early = False  # reset for final check below

    # Run all property checks
    all_props: list[MinedProperty] = [
        _check_type_consistent(outputs),
        _check_never_none(outputs),
        _check_no_nan(outputs),
        _check_non_negative(outputs),
        _check_bounded_01(outputs),
        _check_never_empty(outputs),
        _check_deterministic(fn, inputs),
        _check_idempotent(fn, outputs, inputs),
        _check_involution(fn, outputs, inputs),
        _check_observed_bounds(outputs),
        _check_sorted(outputs),
        _check_constant_output(outputs),
        _check_output_length_constant(outputs),
        _check_bijective(inputs, outputs),
        _check_preserves_type(inputs, outputs),
        _check_null_on_null(fn, inputs),
    ]
    all_props.extend(_check_monotonic(inputs, outputs))
    all_props.extend(_check_length_relationship(inputs, outputs))
    all_props.extend(_check_output_subset_of_input(inputs, outputs))
    all_props.extend(_check_linear_relationship(inputs, outputs))
    all_props.append(_check_commutative(fn, inputs, outputs))
    all_props.append(_check_associative(fn, inputs))

    # Separate applicable (total > 0) from not-applicable (total == 0)
    suppressed = _suppressed_names(list(ignore_properties))
    props = [
        p for p in all_props if p.total > 0 and _normalize_property_token(p.name) not in suppressed
    ]
    not_applicable = [
        p.name
        for p in all_props
        if p.total == 0 and _normalize_property_token(p.name) not in suppressed
    ]

    # Enrich counterexamples with actual input/output values.
    # Many checkers only record the index — we have the actual data.
    for p in props:
        if p.counterexample and "index" in p.counterexample:
            idx = p.counterexample["index"]
            if 0 <= idx < len(inputs):
                p.counterexample["input"] = inputs[idx]
            if 0 <= idx < len(outputs):
                p.counterexample["output"] = outputs[idx]

    for prop in props:
        if minimize_findings and _is_suspicious_property(prop):
            _minimize_and_replay_property(
                fn,
                prop,
                strategies,
                max_examples=max_examples,
            )

    # Coverage saturation: if the last 50%+ of examples found no new edges,
    # more compute won't help — the input space is saturated for this function.
    is_saturated = len(edges_seen) > 0 and stale_count > max(len(outputs) // 2, 10)

    name = getattr(fn, "__name__", str(fn))
    return MineResult(
        function=name,
        examples=len(outputs),
        properties=props,
        not_applicable=not_applicable,
        collected_inputs=inputs,
        collected_outputs=outputs,
        edges_discovered=len(edges_seen),
        saturated=is_saturated,
        branch_points=branch_points,
        branches_cracked=new_edge_count,
    )
