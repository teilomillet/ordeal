from __future__ import annotations
# ruff: noqa


@dataclass(frozen=True)
class _ValidationSampleMatrix:
    """Immutable validation templates that are cloned before every replay."""

    samples: tuple[tuple[tuple[str, object], ...], ...]
    sha256: str

    def replay(self) -> list[dict[str, object]]:
        """Return isolated samples so one mutant cannot taint another."""
        return [copy.deepcopy(dict(sample)) for sample in self.samples]


def _validation_sample_matrix(
    original_func: Callable,
    original: "MineResult",
    max_examples: int,
) -> _ValidationSampleMatrix:
    """Build one deterministic sample matrix from the original callable."""
    budget = max(20, max_examples)
    collected_inputs = getattr(original, "collected_inputs", ())
    samples: list[dict[str, object]] = [dict(kwargs) for kwargs in collected_inputs[:budget]]

    if len(samples) < budget:
        plan = _equivalence_sample_plan(original_func, budget - len(samples))
        if plan is not None:
            try:
                sig = inspect.signature(original_func)
            except (TypeError, ValueError):
                sig = None

            if sig is not None:
                params = [
                    p.name
                    for p in sig.parameters.values()
                    if p.name != "self" and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                if params:
                    for args in plan.samples:
                        if len(args) == len(params):
                            samples.append(dict(zip(params, args, strict=False)))

    frozen_samples = tuple(
        tuple((str(name), copy.deepcopy(value)) for name, value in sample.items())
        for sample in samples
    )
    canonical = json.dumps(
        [dict(sample) for sample in frozen_samples],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=repr,
    ).encode("utf-8")
    return _ValidationSampleMatrix(
        samples=frozen_samples,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


@functools.lru_cache(maxsize=128)
def _compile_mined_property_evaluator(
    property_names: tuple[str, ...],
) -> Callable[[Callable, list[dict[str, object]]], list[str]]:
    """Compile and cache the full mined-property evaluator for one property set."""
    from ordeal.mine import (
        _check_associative,
        _check_bijective,
        _check_bounded_01,
        _check_commutative,
        _check_constant_output,
        _check_deterministic,
        _check_idempotent,
        _check_involution,
        _check_length_relationship,
        _check_linear_relationship,
        _check_monotonic,
        _check_never_empty,
        _check_never_none,
        _check_no_nan,
        _check_non_negative,
        _check_null_on_null,
        _check_observed_bounds,
        _check_output_length_constant,
        _check_output_subset_of_input,
        _check_preserves_type,
        _check_sorted,
        _check_type_consistent,
    )

    def evaluate(
        current_func: Callable,
        sample_inputs: list[dict[str, object]],
    ) -> list[str]:
        replayed_inputs: list[dict[str, object]] = []
        outputs: list[object] = []
        for kwargs in sample_inputs:
            try:
                output = current_func(**kwargs)
            except Exception as exc:
                raise AssertionError(f"Mutant raised on replayed mined input {kwargs!r}") from exc
            replayed_inputs.append(dict(kwargs))
            outputs.append(output)

        all_props = [
            _check_type_consistent(outputs),
            _check_never_none(outputs),
            _check_no_nan(outputs),
            _check_non_negative(outputs),
            _check_bounded_01(outputs),
            _check_never_empty(outputs),
            _check_deterministic(current_func, replayed_inputs),
            _check_idempotent(current_func, outputs, replayed_inputs),
            _check_involution(current_func, outputs, replayed_inputs),
            _check_observed_bounds(outputs),
            _check_sorted(outputs),
            _check_constant_output(outputs),
            _check_output_length_constant(outputs),
            _check_bijective(replayed_inputs, outputs),
            _check_preserves_type(replayed_inputs, outputs),
            _check_null_on_null(current_func, replayed_inputs),
            _check_commutative(current_func, replayed_inputs, outputs),
            _check_associative(current_func, replayed_inputs),
        ]
        all_props.extend(_check_monotonic(replayed_inputs, outputs))
        all_props.extend(_check_length_relationship(replayed_inputs, outputs))
        all_props.extend(_check_output_subset_of_input(replayed_inputs, outputs))
        all_props.extend(_check_linear_relationship(replayed_inputs, outputs))
        applicable = [prop for prop in all_props if prop.total > 0]

        failed: list[str] = []
        for property_name in property_names:
            match = next((prop for prop in applicable if prop.name == property_name), None)
            if match is None or not match.universal:
                failed.append(property_name)
        return failed

    return evaluate
