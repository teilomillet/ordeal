from __future__ import annotations
# ruff: noqa
def _is_runtime_equivalent(
    original: Callable,
    mutant_fn: Callable,
    n_samples: int = 10,
) -> bool:
    """Heuristic: run both functions on random inputs, skip if outputs match.

    Tests boundary values first (0, 1, -1, etc.) to catch mutations that
    only differ at edges (e.g. ``<`` → ``<=``), then *n_samples* random
    draws from type-driven strategies.

    Returns ``True`` (skip) when all samples agree.  Falls back to ``False``
    (test it) when inputs can't be generated or any sample disagrees.
    """
    plan = _equivalence_sample_plan(original, n_samples)
    if plan is None:
        return False

    for args in plan.samples:
        original_args = _clone_equivalence_args(args)
        mutant_args = _clone_equivalence_args(args)
        try:
            if original(*original_args) != mutant_fn(*mutant_args):
                return False
        except Exception:
            return False

    return True
def _clone_equivalence_args(args: tuple[object, ...]) -> list[object]:
    """Copy runtime-equivalence inputs so one call cannot taint the next."""
    try:
        return copy.deepcopy(list(args))
    except Exception:
        return list(args)
@functools.lru_cache(maxsize=128)
def _equivalence_sample_plan(
    original: Callable,
    n_samples: int,
) -> _EquivalenceSamplePlan | None:
    """Prepare deterministic sample inputs for runtime-equivalence checks."""
    import warnings

    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    try:
        sig = inspect.signature(original)
    except (ValueError, TypeError):
        return None

    params = [
        p
        for p in sig.parameters.values()
        if p.name != "self" and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if not params:
        return _EquivalenceSamplePlan(samples=((),))

    try:
        from ordeal.quickcheck import strategy_for_type

        hints = safe_get_annotations(original)
    except Exception:
        return None

    param_hints: list[object] = []
    strategies: list[object] = []
    for p in params:
        if p.name not in hints:
            return None
        hint = hints[p.name]
        param_hints.append(hint)
        try:
            strategies.append(strategy_for_type(hint))
        except Exception:
            return None

    boundary_lists: list[list[object]] = []
    for hint in param_hints:
        origin = getattr(hint, "__origin__", hint)
        boundary_lists.append(list(_BOUNDARY_VALUES.get(origin, [])))

    samples: list[tuple[object, ...]] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            generated: list[tuple[object, ...]] = []

            @given(st.tuples(*strategies))
            @settings(
                max_examples=max(1, n_samples),
                database=None,
                derandomize=True,
                deadline=None,
                suppress_health_check=list(HealthCheck),
            )
            def collect(sample: tuple[object, ...]) -> None:
                generated.append(tuple(sample))

            collect()
            defaults: list[object] = []
            for i, values in enumerate(boundary_lists):
                if values:
                    defaults.append(values[0])
                elif generated:
                    defaults.append(generated[0][i])
                else:
                    return None

            for i, values in enumerate(boundary_lists):
                for value in values:
                    args = list(defaults)
                    args[i] = value
                    samples.append(tuple(args))

            samples.extend(generated[:n_samples])
    except Exception:
        return None

    return _EquivalenceSamplePlan(samples=tuple(samples))
def _auto_test_fn(target: str, test_filter: str | None = None) -> Callable[[], None]:
    """Create a test function that runs pytest in-process for *target*.

    When *test_filter* is provided, it is passed as ``-k`` to pytest,
    replacing the default broad module-name filter.  This avoids running
    the entire test suite for each mutant when only a few tests are
    relevant (e.g. ``test_filter="test_postprocess"`` instead of
    matching all 1555 tests).

    Runs in-process so PatchFault swaps are visible to the test code.
    """

    def run_tests() -> None:
        import pytest

        selection = _mutation_test_selection(target, test_filter=test_filter)
        with _disable_seed_replay():
            rc = pytest.main(
                [
                    "-x",
                    "-q",
                    "--tb=short",
                    "--no-header",
                    "--chaos",
                    "-o",
                    "addopts=",
                    *selection.pytest_args(),
                ]
            )
        if rc == 5:
            _raise_no_tests_found(target)
        if rc != 0:
            raise AssertionError(f"pytest returned exit code {rc}")

    return run_tests
