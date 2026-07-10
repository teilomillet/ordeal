from __future__ import annotations
# ruff: noqa
@dataclass
class MineModuleResult:
    """Results of mining an entire module for both per-function and cross-function properties.

    Combines individual ``MineResult`` per function with ``CrossFunctionProperty``
    relationships discovered between function pairs.  The ``summary()`` method
    produces a single report covering both, so developers and AI assistants can
    see the full picture at a glance.

    Attributes:
        module: Dotted module path (e.g. ``"myapp.scoring"``).
        per_function: Individual mining results keyed by function name.
        cross_function: Relationships discovered between function pairs.
    """

    module: str
    per_function: dict[str, MineResult] = field(default_factory=dict)
    cross_function: list[CrossFunctionProperty] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable report covering per-function and cross-function properties.

        Per-function properties are listed first (one section per function),
        followed by cross-function relationships grouped by confidence.
        """
        lines = [f"mine_module({self.module})"]
        lines.append(
            f"  {len(self.per_function)} functions, "
            f"{len(self.cross_function)} cross-function relationships"
        )
        lines.append("")

        # Per-function summaries
        for name in sorted(self.per_function):
            result = self.per_function[name]
            lines.append(result.summary())
            lines.append("")

        # Cross-function relationships
        if self.cross_function:
            lines.append("Cross-function relationships:")
            for prop in sorted(self.cross_function, key=lambda p: -p.confidence):
                if prop.total > 0:
                    lines.append(str(prop))
        else:
            lines.append("Cross-function relationships: none discovered")

        return "\n".join(lines)
# ============================================================================
# Property checkers — each returns (name, holds: bool)
# ============================================================================


def _check_type_consistent(
    outputs: list[Any],
) -> MinedProperty:
    """All outputs have the same type."""
    if not outputs:
        return MinedProperty("output type consistent", 0, 0)
    first_type = type(outputs[0])
    holds = sum(1 for o in outputs if type(o) is first_type)
    ce = None
    if holds < len(outputs):
        for i, o in enumerate(outputs):
            if type(o) is not first_type:
                ce = {"index": i, "expected": first_type.__name__, "got": type(o).__name__}
                break
    return MinedProperty(f"output type is {first_type.__name__}", holds, len(outputs), ce)
def _check_never_none(outputs: list[Any]) -> MinedProperty:
    """Output is never None."""
    holds = 0
    ce = None
    for i, output in enumerate(outputs):
        if output is not None:
            holds += 1
        elif ce is None:
            ce = {"index": i}
    return MinedProperty("never None", holds, len(outputs), ce)
def _check_no_nan(outputs: list[Any]) -> MinedProperty:
    """Output never contains NaN (for floats)."""
    total = 0
    holds = 0
    ce = None
    for index, o in enumerate(outputs):
        if isinstance(o, float):
            total += 1
            if not math.isnan(o):
                holds += 1
            elif ce is None:
                ce = {"index": index, "value": o}
        elif hasattr(o, "shape"):
            total += 1
            try:
                import numpy as np

                if not np.any(np.isnan(o)):
                    holds += 1
                elif ce is None:
                    ce = {"index": index, "value": o}
            except (ImportError, TypeError):
                holds += 1
    if total == 0:
        return MinedProperty("no NaN", len(outputs), len(outputs))
    return MinedProperty("no NaN", holds, total, ce)
def _check_non_negative(outputs: list[Any]) -> MinedProperty:
    """Output is always >= 0 (for numeric outputs)."""
    total = 0
    holds = 0
    ce = None
    for i, o in enumerate(outputs):
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            total += 1
            if o >= 0:
                holds += 1
            elif ce is None:
                ce = {"index": i, "value": o}
    if total == 0:
        return MinedProperty("output >= 0", 0, 0)
    return MinedProperty("output >= 0", holds, total, ce)
def _check_bounded_01(outputs: list[Any]) -> MinedProperty:
    """Output is always in [0, 1]."""
    total = 0
    holds = 0
    ce = None
    for i, o in enumerate(outputs):
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            total += 1
            if 0.0 <= o <= 1.0:
                holds += 1
            elif ce is None:
                ce = {"index": i, "value": o}
    if total == 0:
        return MinedProperty("output in [0, 1]", 0, 0)
    return MinedProperty("output in [0, 1]", holds, total, ce)
def _check_never_empty(outputs: list[Any]) -> MinedProperty:
    """Output is never empty (for sequences/strings)."""
    total = 0
    holds = 0
    ce = None
    for i, o in enumerate(outputs):
        if isinstance(o, (list, tuple, str, dict)):
            total += 1
            if len(o) > 0:
                holds += 1
            elif ce is None:
                ce = {"index": i}
    if total == 0:
        return MinedProperty("never empty", 0, 0)
    return MinedProperty("never empty", holds, total, ce)
def _check_deterministic(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """Same input always gives the same output."""
    total = 0
    holds = 0
    ce = None
    for kwargs in inputs[:50]:  # cap at 50 to keep runtime sane
        try:
            out1 = _call_sync(fn, **kwargs)
            out2 = _call_sync(fn, **kwargs)
            total += 1
            if _approx_equal(out1, out2):
                holds += 1
            elif ce is None:
                ce = {"input": kwargs, "first": out1, "second": out2}
        except Exception:
            pass
    if total == 0:
        return MinedProperty("deterministic", 0, 0)
    return MinedProperty("deterministic", holds, total, ce)
def _check_idempotent(
    fn: Callable[..., Any],
    outputs: list[Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """f(f(x)) == f(x) — only when input and output types align."""
    if not outputs or not inputs:
        return MinedProperty("idempotent", 0, 0)

    # Get first parameter name to feed output back as input
    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if not params:
        return MinedProperty("idempotent", 0, 0)

    first_param = params[0]
    total = 0
    holds = 0
    ce = None
    for i, (output, kwargs) in enumerate(zip(outputs[:30], inputs[:30])):
        if output is None:
            continue
        try:
            kwargs2 = dict(kwargs)
            kwargs2[first_param] = output
            out2 = _call_sync(fn, **kwargs2)
            total += 1
            if _approx_equal(out2, output):
                holds += 1
            elif ce is None:
                ce = {"index": i, "input": kwargs, "output": output, "replayed": out2}
        except (TypeError, ValueError, AttributeError):
            pass  # output type doesn't fit as input — skip
    if total == 0:
        return MinedProperty("idempotent", 0, 0)
    return MinedProperty("idempotent", holds, total, ce)
def _check_involution(
    fn: Callable[..., Any],
    outputs: list[Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """f(f(x)) == x — only when input and output types align."""
    if not outputs or not inputs:
        return MinedProperty("involution", 0, 0)

    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if not params:
        return MinedProperty("involution", 0, 0)

    first_param = params[0]
    total = 0
    holds = 0
    ce = None
    for i, (output, kwargs) in enumerate(zip(outputs[:30], inputs[:30])):
        if output is None:
            continue
        try:
            kwargs2 = dict(kwargs)
            kwargs2[first_param] = output
            out2 = _call_sync(fn, **kwargs2)
            total += 1
            if _approx_equal(out2, kwargs[first_param]):
                holds += 1
            elif ce is None:
                ce = {"index": i, "input": kwargs, "output": output, "replayed": out2}
        except (TypeError, ValueError, AttributeError):
            pass
    if total == 0:
        return MinedProperty("involution", 0, 0)
    return MinedProperty("involution", holds, total, ce)
def _check_monotonic(
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> list[MinedProperty]:
    """Check if output is monotonic w.r.t. each numeric input parameter."""
    if len(inputs) < 2 or len(outputs) < 2:
        return []

    results: list[MinedProperty] = []
    param_names = list(inputs[0].keys())
    multi = len(param_names) > 1

    for param_name in param_names:
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
                pairs.append((v, out))

        if len(pairs) < 2:
            continue

        pairs.sort()
        total = len(pairs) - 1
        nd = sum(1 for i in range(total) if pairs[i][1] <= pairs[i + 1][1])
        ni = sum(1 for i in range(total) if pairs[i][1] >= pairs[i + 1][1])

        suffix = f" in {param_name}" if multi else ""
        if nd >= ni:
            results.append(MinedProperty(f"monotonically non-decreasing{suffix}", nd, total))
        else:
            results.append(MinedProperty(f"monotonically non-increasing{suffix}", ni, total))

    return results
def _check_observed_bounds(outputs: list[Any]) -> MinedProperty:
    """Report the observed min/max range of numeric outputs."""
    nums = [
        o
        for o in outputs
        if isinstance(o, (int, float)) and not isinstance(o, bool) and math.isfinite(o)
    ]
    if not nums:
        return MinedProperty("observed range", 0, 0)

    lo, hi = min(nums), max(nums)

    def _fmt(v: int | float) -> str:
        if isinstance(v, int):
            return str(v)
        return f"{v:.4g}"

    return MinedProperty(f"observed range [{_fmt(lo)}, {_fmt(hi)}]", len(nums), len(nums))
def _check_length_relationship(
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> list[MinedProperty]:
    """Check if output length relates to input length."""
    if not inputs or not outputs:
        return []

    results: list[MinedProperty] = []

    for param_name in inputs[0]:
        eq = le = ge = total = 0
        for inp, out in zip(inputs, outputs):
            try:
                in_len = len(inp[param_name])
                out_len = len(out)
            except TypeError:
                continue
            total += 1
            if out_len == in_len:
                eq += 1
            if out_len <= in_len:
                le += 1
            if out_len >= in_len:
                ge += 1

        if total == 0:
            continue

        results.append(MinedProperty(f"len(output) == len({param_name})", eq, total))
        if le > eq:
            results.append(MinedProperty(f"len(output) <= len({param_name})", le, total))
        if ge > eq:
            results.append(MinedProperty(f"len(output) >= len({param_name})", ge, total))

    return results
_GENERIC_OPERAND_NAMES = {
    "a",
    "b",
    "left",
    "right",
    "lhs",
    "rhs",
    "x",
    "y",
    "first",
    "second",
}
def _operands_look_interchangeable(fn: Callable[..., Any]) -> bool:
    """Return True when a 2-arg function looks symmetric enough to mine laws."""
    import inspect

    hints = safe_get_annotations(fn)

    sig = inspect.signature(fn)
    params = [name for name in sig.parameters if name not in ("self", "cls")]
    if len(params) != 2:
        return False

    normalized = [name.lower().rstrip("0123456789_") for name in params]
    if all(name in _GENERIC_OPERAND_NAMES for name in normalized):
        first_type = hints.get(params[0])
        second_type = hints.get(params[1])
        return first_type is None or second_type is None or first_type == second_type

    return normalized[0] == normalized[1]
def _check_commutative(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> MinedProperty:
    """Check if f(a, b) == f(b, a) for 2-parameter functions."""
    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if len(params) != 2 or not _operands_look_interchangeable(fn):
        return MinedProperty("commutative", 0, 0)

    a_name, b_name = params
    total = 0
    holds = 0
    ce = None
    for kwargs, out in zip(inputs[:50], outputs[:50]):
        try:
            swapped = {a_name: kwargs[b_name], b_name: kwargs[a_name]}
            out_swapped = _call_sync(fn, **swapped)
            total += 1
            if _approx_equal(out, out_swapped):
                holds += 1
            elif ce is None:
                ce = {
                    "input": kwargs,
                    "output": out,
                    "swapped_input": swapped,
                    "swapped_output": out_swapped,
                }
        except Exception:
            pass
    if total == 0:
        return MinedProperty("commutative", 0, 0)
    return MinedProperty("commutative", holds, total, ce)
def _check_associative(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """Check if f(a, f(b, c)) == f(f(a, b), c) for 2-parameter functions."""
    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if len(params) != 2 or not _operands_look_interchangeable(fn):
        return MinedProperty("associative", 0, 0)

    a_name, b_name = params
    total = 0
    holds = 0
    ce = None
    # Take triples of values from the first parameter
    for i in range(0, min(len(inputs) - 2, 30), 3):
        a = inputs[i][a_name]
        b = inputs[i + 1][a_name]
        c = inputs[i + 2][a_name]
        try:
            bc = _call_sync(fn, **{a_name: b, b_name: c})
            left = _call_sync(fn, **{a_name: a, b_name: bc})
            ab = _call_sync(fn, **{a_name: a, b_name: b})
            right = _call_sync(fn, **{a_name: ab, b_name: c})
            total += 1
            if _approx_equal(left, right):
                holds += 1
            elif ce is None:
                ce = {"input": {a_name: a, b_name: b, "third": c}, "left": left, "right": right}
        except Exception:
            pass
    if total == 0:
        return MinedProperty("associative", 0, 0)
    return MinedProperty("associative", holds, total, ce)
def _check_output_subset_of_input(
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> list[MinedProperty]:
    """Check if the output collection is always a subset of an input collection."""
    if not inputs or not outputs:
        return []

    results: list[MinedProperty] = []
    for param_name in inputs[0]:
        total = 0
        holds = 0
        ce = None
        for inp, out in zip(inputs, outputs):
            v = inp[param_name]
            if not isinstance(v, (list, tuple, set, frozenset)):
                continue
            if not isinstance(out, (list, tuple, set, frozenset)):
                continue
            total += 1
            try:
                if set(out) <= set(v):
                    holds += 1
                elif ce is None:
                    extra = set(out) - set(v)
                    ce = {
                        param_name: v,
                        "output": out,
                        "extra_in_output": extra,
                    }
            except TypeError:
                # unhashable elements — skip
                total -= 1

        if total == 0:
            continue
        results.append(MinedProperty(f"output subset of {param_name}", holds, total, ce))
    return results
