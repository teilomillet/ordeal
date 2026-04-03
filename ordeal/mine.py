"""Property mining — discover invariants from execution traces.

Run a function many times with random inputs, observe patterns in
outputs, and report likely properties.  The user confirms which are
real — turning observed regularities into tested invariants::

    from ordeal.mine import mine

    properties = mine(my_function, max_examples=500)
    for p in properties:
        print(p)
    # output >= 0: 100% (500/500)
    # output is float: 100% (500/500)
    # deterministic: 100% (500/500)
    # output in [0, 1]: 98% (490/500)
"""

from __future__ import annotations

import importlib
import inspect
import math
import random as _random
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, get_args, get_origin, get_type_hints

import hypothesis.strategies as st
from hypothesis import given, settings

from ordeal.auto import _get_public_functions, _infer_strategies

_REL_TOL = 1e-9
_ABS_TOL = 1e-12


def _approx_equal(a: Any, b: Any) -> bool:
    """Equality that tolerates float rounding.

    Uses exact ``==`` for non-float types.  For floats, applies
    ``math.isclose`` with tight tolerances so only rounding noise is
    forgiven — genuinely different values still compare as unequal.
    """
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return False
        return math.isclose(a, b, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    try:
        result = a == b
        # numpy arrays return an array from ==; reduce to scalar
        if hasattr(result, "__iter__") and not isinstance(result, str):
            import numpy as np

            return bool(np.all(np.asarray(result)))
        return bool(result)
    except Exception:
        return False


@dataclass
class MinedProperty:
    """A likely property observed during mining."""

    name: str
    holds: int
    total: int
    counterexample: dict[str, Any] | None = None

    @property
    def confidence(self) -> float:
        """Fraction of examples where the property held."""
        return self.holds / self.total if self.total > 0 else 0.0

    @property
    def universal(self) -> bool:
        """True if the property held for every example."""
        return self.holds == self.total

    def __str__(self) -> str:
        pct = f"{self.confidence:.0%}"
        status = "ALWAYS" if self.universal else pct
        return f"  {status:>6}  {self.name} ({self.holds}/{self.total})"


@dataclass
class CrossFunctionProperty:
    """A relationship discovered between two functions.

    Cross-function properties capture structural relationships that no
    single-function analysis can find.  These are the most valuable
    properties for regression testing because they encode *contracts*
    between components:

    - **roundtrip**: ``g(f(x)) == x`` — encoding/decoding, serialize/deserialize,
      compress/decompress.  If this breaks, data is being lost or corrupted.
    - **commutative_composition**: ``f(g(x)) == g(f(x))`` — the two functions
      can be applied in either order.  Rare but powerful when it holds.
    - **equivalent**: ``f(x) == g(x)`` — both produce identical output for
      all tested inputs.  Often signals duplicate implementations, or a
      fast-path that should match a reference implementation.

    Attributes:
        function_a: Qualified name of the first function.
        function_b: Qualified name of the second function.
        relation: Kind of relationship: ``"roundtrip"``,
            ``"commutative_composition"``, or ``"equivalent"``.
        confidence: Fraction of tested inputs where the relation held
            (0.0 to 1.0).
        holds: Number of inputs where the relation held.
        total: Number of inputs tested.
        counterexample: If the relation failed, one example showing the
            disagreement.  ``None`` when the relation held universally.
    """

    function_a: str
    function_b: str
    relation: str
    confidence: float
    holds: int
    total: int
    counterexample: dict[str, Any] | None = None

    def __str__(self) -> str:
        pct = f"{self.confidence:.0%}"
        status = "ALWAYS" if self.confidence == 1.0 and self.total > 0 else pct
        label = f"{self.function_a} <-> {self.function_b}: {self.relation}"
        return f"  {status:>6}  {label} ({self.holds}/{self.total})"


# Properties that mine() structurally cannot check.
# These are always "unknown unknowns" from mine()'s perspective.
# Stating them explicitly turns them into "known unknowns" for the user.
STRUCTURAL_LIMITATIONS: list[str] = [
    "output value correctness (fuzz checks crash safety, not behavior)",
    "cross-function consistency (e.g., batch == map of individual)",
    "domain-specific invariants (e.g., weighted sum, refusal detection)",
    "error handling for intentionally invalid inputs",
    "performance and resource usage",
    "concurrency and thread safety",
    "state mutation and side effects",
    "higher-arity algebraic laws (checked for 2-param functions only)",
]
"""Things mine() fundamentally cannot discover from random sampling.

These are not bugs in mine() — they require domain knowledge that
no automated tool can infer.  Stating them explicitly helps the
developer know what manual tests to write.
"""


@dataclass
class MineResult:
    """All properties discovered for a function.

    Separates what was checked into three categories:

    - ``properties``: checked and applicable (total > 0)
    - ``not_applicable``: checked but not relevant to this function
      (e.g., "bounded [0,1]" for a function returning strings)
    - ``not_checked``: structural limitations — things mine()
      fundamentally cannot verify (always the same list)
    """

    function: str
    examples: int
    properties: list[MinedProperty] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=lambda: list(STRUCTURAL_LIMITATIONS))
    collected_inputs: list[dict[str, object]] = field(default_factory=list, repr=False)
    collected_outputs: list[object] = field(default_factory=list, repr=False)
    edges_discovered: int = 0
    saturated: bool = False
    branch_points: dict[str, list[object]] = field(default_factory=dict)
    branches_cracked: int = 0

    @property
    def universal(self) -> list[MinedProperty]:
        """Properties that held on every single example."""
        return [p for p in self.properties if p.universal]

    @property
    def likely(self) -> list[MinedProperty]:
        """Properties with >= 95% confidence but not universal."""
        return [p for p in self.properties if 0.95 <= p.confidence < 1.0]

    def summary(self) -> str:
        """Human-readable report."""
        header = f"mine({self.function}): {self.examples} examples"
        if self.edges_discovered:
            header += f", {self.edges_discovered} edges"
        if self.branch_points:
            total_bp = sum(len(v) for v in self.branch_points.values())
            header += f", {total_bp} branch points"
            if self.branches_cracked:
                header += f" ({self.branches_cracked} cracked)"
        if self.saturated:
            header += " (saturated)"
        lines = [header]
        for p in sorted(self.properties, key=lambda p: -p.confidence):
            if p.confidence >= 0.5:
                lines.append(str(p))
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)


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
    holds = sum(1 for o in outputs if o is not None)
    return MinedProperty("never None", holds, len(outputs))


def _check_no_nan(outputs: list[Any]) -> MinedProperty:
    """Output never contains NaN (for floats)."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, float):
            total += 1
            if not math.isnan(o):
                holds += 1
        elif hasattr(o, "shape"):
            total += 1
            try:
                import numpy as np

                if not np.any(np.isnan(o)):
                    holds += 1
            except (ImportError, TypeError):
                holds += 1
    if total == 0:
        return MinedProperty("no NaN", len(outputs), len(outputs))
    return MinedProperty("no NaN", holds, total)


def _check_non_negative(outputs: list[Any]) -> MinedProperty:
    """Output is always >= 0 (for numeric outputs)."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            total += 1
            if o >= 0:
                holds += 1
    if total == 0:
        return MinedProperty("output >= 0", 0, 0)
    return MinedProperty("output >= 0", holds, total)


def _check_bounded_01(outputs: list[Any]) -> MinedProperty:
    """Output is always in [0, 1]."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            total += 1
            if 0.0 <= o <= 1.0:
                holds += 1
    if total == 0:
        return MinedProperty("output in [0, 1]", 0, 0)
    return MinedProperty("output in [0, 1]", holds, total)


def _check_never_empty(outputs: list[Any]) -> MinedProperty:
    """Output is never empty (for sequences/strings)."""
    total = 0
    holds = 0
    for o in outputs:
        if isinstance(o, (list, tuple, str, dict)):
            total += 1
            if len(o) > 0:
                holds += 1
    if total == 0:
        return MinedProperty("never empty", 0, 0)
    return MinedProperty("never empty", holds, total)


def _check_deterministic(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """Same input always gives the same output."""
    total = 0
    holds = 0
    for kwargs in inputs[:50]:  # cap at 50 to keep runtime sane
        try:
            out1 = fn(**kwargs)
            out2 = fn(**kwargs)
            total += 1
            if _approx_equal(out1, out2):
                holds += 1
        except Exception:
            pass
    if total == 0:
        return MinedProperty("deterministic", 0, 0)
    return MinedProperty("deterministic", holds, total)


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
    for i, (output, kwargs) in enumerate(zip(outputs[:30], inputs[:30])):
        if output is None:
            continue
        try:
            kwargs2 = dict(kwargs)
            kwargs2[first_param] = output
            out2 = fn(**kwargs2)
            total += 1
            if _approx_equal(out2, output):
                holds += 1
        except (TypeError, ValueError, AttributeError):
            pass  # output type doesn't fit as input — skip
    if total == 0:
        return MinedProperty("idempotent", 0, 0)
    return MinedProperty("idempotent", holds, total)


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
    for output, kwargs in zip(outputs[:30], inputs[:30]):
        if output is None:
            continue
        try:
            kwargs2 = dict(kwargs)
            kwargs2[first_param] = output
            out2 = fn(**kwargs2)
            total += 1
            if _approx_equal(out2, kwargs[first_param]):
                holds += 1
        except (TypeError, ValueError, AttributeError):
            pass
    if total == 0:
        return MinedProperty("involution", 0, 0)
    return MinedProperty("involution", holds, total)


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


def _check_commutative(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
    outputs: list[Any],
) -> MinedProperty:
    """Check if f(a, b) == f(b, a) for 2-parameter functions."""
    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if len(params) != 2:
        return MinedProperty("commutative", 0, 0)

    a_name, b_name = params
    total = 0
    holds = 0
    for kwargs, out in zip(inputs[:50], outputs[:50]):
        try:
            swapped = {a_name: kwargs[b_name], b_name: kwargs[a_name]}
            out_swapped = fn(**swapped)
            total += 1
            if _approx_equal(out, out_swapped):
                holds += 1
        except Exception:
            pass
    if total == 0:
        return MinedProperty("commutative", 0, 0)
    return MinedProperty("commutative", holds, total)


def _check_associative(
    fn: Callable[..., Any],
    inputs: list[dict[str, Any]],
) -> MinedProperty:
    """Check if f(a, f(b, c)) == f(f(a, b), c) for 2-parameter functions."""
    import inspect

    sig = inspect.signature(fn)
    params = [n for n in sig.parameters if n not in ("self", "cls")]
    if len(params) != 2:
        return MinedProperty("associative", 0, 0)

    a_name, b_name = params
    total = 0
    holds = 0
    # Take triples of values from the first parameter
    for i in range(0, min(len(inputs) - 2, 30), 3):
        a = inputs[i][a_name]
        b = inputs[i + 1][a_name]
        c = inputs[i + 2][a_name]
        try:
            bc = fn(**{a_name: b, b_name: c})
            left = fn(**{a_name: a, b_name: bc})
            ab = fn(**{a_name: a, b_name: b})
            right = fn(**{a_name: ab, b_name: c})
            total += 1
            if _approx_equal(left, right):
                holds += 1
        except Exception:
            pass
    if total == 0:
        return MinedProperty("associative", 0, 0)
    return MinedProperty("associative", holds, total)


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


def _check_sorted(outputs: list[Any]) -> MinedProperty:
    """Check if the output is always sorted (for list returns)."""
    total = 0
    holds = 0
    for o in outputs:
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
        except TypeError:
            # uncomparable elements — skip this sample
            total -= 1
    if total == 0:
        return MinedProperty("output is sorted", 0, 0)
    return MinedProperty("output is sorted", holds, total)


def _check_constant_output(outputs: list[Any]) -> MinedProperty:
    """Check if the function always returns the same value regardless of input."""
    if len(outputs) < 2:
        return MinedProperty("constant output", 0, 0)
    first = outputs[0]
    holds = sum(1 for o in outputs if _approx_equal(o, first))
    return MinedProperty("constant output", holds, len(outputs))


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
    else:
        return MinedProperty("bijective", unique_outputs, unique_inputs)


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
            result = fn(**base_kwargs)
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
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            nonlocal new_edge_count, stale_count
            if collector:
                collector.start()
            try:
                result = fn(**kwargs)
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
                    result = fn(**mutated)
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
    props = [p for p in all_props if p.total > 0]
    not_applicable = [p.name for p in all_props if p.total == 0]

    # Enrich counterexamples with actual input/output values.
    # Many checkers only record the index — we have the actual data.
    for p in props:
        if p.counterexample and "index" in p.counterexample:
            idx = p.counterexample["index"]
            if 0 <= idx < len(inputs):
                p.counterexample["input"] = inputs[idx]
            if 0 <= idx < len(outputs):
                p.counterexample["output"] = outputs[idx]

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


def mine_pair(
    f: Callable[..., Any],
    g: Callable[..., Any],
    *,
    max_examples: int = 200,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> MineResult:
    """Discover relational properties between two functions.

    Checks roundtrip (``g(f(x)) == x``), the reverse (``f(g(x)) == x``),
    and commutative composition (``f(g(x)) == g(f(x))``).  Strategies
    are inferred from *f*'s signature.

    Example::

        result = mine_pair(json.dumps, json.loads)
        # discovers: roundtrip g(f(x)) == x

        result = mine_pair(encode, decode)
        # discovers: roundtrip g(f(x)) == x, roundtrip f(g(x)) == x
    """
    import inspect

    from ordeal.auto import _unwrap

    f = _unwrap(f)
    g = _unwrap(g)

    # Normalize fixtures
    normalized: dict[str, st.SearchStrategy[Any]] | None = None
    if fixtures:
        normalized = {}
        for k, v in fixtures.items():
            normalized[k] = v if isinstance(v, st.SearchStrategy) else st.just(v)

    strategies = _infer_strategies(f, normalized)
    if strategies is None:
        fname = getattr(f, "__name__", str(f))
        raise ValueError(f"Cannot infer strategies for {fname}.")

    # Get first param name for feeding output back
    sig_f = inspect.signature(f)
    params_f = [n for n in sig_f.parameters if n not in ("self", "cls")]
    sig_g = inspect.signature(g)
    params_g = [n for n in sig_g.parameters if n not in ("self", "cls")]
    first_f = params_f[0] if params_f else None
    first_g = params_g[0] if params_g else None

    # Collect inputs and outputs of f
    inputs: list[dict[str, Any]] = []
    outputs_f: list[Any] = []

    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            out = f(**kwargs)
            outputs_f.append(out)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    fname = getattr(f, "__name__", str(f))
    gname = getattr(g, "__name__", str(g))
    pair_name = f"{fname} <-> {gname}"
    cap = min(len(inputs), 50)

    # -- roundtrip: g(f(x)) == x --
    rt_holds = rt_total = 0
    for kwargs, out_f in zip(inputs[:cap], outputs_f[:cap]):
        if out_f is None or first_g is None:
            continue
        try:
            back = g(**{first_g: out_f})
            rt_total += 1
            if _approx_equal(back, kwargs[first_f]):
                rt_holds += 1
        except Exception:
            pass

    # -- reverse roundtrip: f(g(x)) == x --
    # Generate inputs for g, then feed through f
    rr_holds = rr_total = 0
    strategies_g = _infer_strategies(g, normalized)
    if strategies_g and first_f and first_g:
        g_inputs: list[dict[str, Any]] = []
        g_outputs: list[Any] = []
        try:

            @given(**strategies_g)
            @settings(max_examples=max_examples, database=None)
            def collect_g(**kwargs: Any) -> None:
                out = g(**kwargs)
                g_outputs.append(out)
                g_inputs.append(dict(kwargs))

            collect_g()
        except Exception:
            pass

        for kwargs_g, out_g in zip(g_inputs[:cap], g_outputs[:cap]):
            if out_g is None:
                continue
            try:
                back = f(**{first_f: out_g})
                rr_total += 1
                if _approx_equal(back, kwargs_g[first_g]):
                    rr_holds += 1
            except Exception:
                pass

    # -- commutative composition: f(g(x)) == g(f(x)) --
    cc_holds = cc_total = 0
    if first_f and first_g:
        for kwargs, out_f in zip(inputs[:cap], outputs_f[:cap]):
            if out_f is None:
                continue
            try:
                fg = g(**{first_g: out_f})  # g(f(x))
                gx = g(**{first_g: kwargs[first_f]})  # g(x)
                gf = f(**{first_f: gx})  # f(g(x))
                cc_total += 1
                if _approx_equal(fg, gf):
                    cc_holds += 1
            except Exception:
                pass

    all_props = [
        MinedProperty(f"roundtrip {gname}({fname}(x)) == x", rt_holds, rt_total),
        MinedProperty(f"roundtrip {fname}({gname}(x)) == x", rr_holds, rr_total),
        MinedProperty("commutative composition", cc_holds, cc_total),
    ]

    props = [p for p in all_props if p.total > 0]
    not_applicable = [p.name for p in all_props if p.total == 0]

    return MineResult(
        function=pair_name,
        examples=max(len(inputs), 0),
        properties=props,
        not_applicable=not_applicable,
    )


# ============================================================================
# Cross-function mining
# ============================================================================


def _return_type(fn: Callable[..., Any]) -> type | None:
    """Extract the return type annotation from a function, or None if absent."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        return None
    return hints.get("return")


def _first_param_type(fn: Callable[..., Any]) -> tuple[str | None, type | None]:
    """Return (name, type) of the first non-self/cls parameter, or (None, None)."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    for name, _param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        return name, hints.get(name)
    return None, None


def _types_compatible(source_type: type | None, target_type: type | None) -> bool:
    """Check whether *source_type* can plausibly be fed into *target_type*.

    Returns ``True`` when the types are identical, when the target is a
    supertype, or when both are the same generic origin (e.g. both
    ``list[...]``).  Returns ``False`` when either is ``None`` (unknown)
    to avoid false positives from untyped code.
    """
    if source_type is None or target_type is None:
        return False
    # Exact match
    if source_type is target_type:
        return True
    # Unwrap Optional / Union — check any branch
    import types as pytypes

    src_origin = get_origin(source_type)
    tgt_origin = get_origin(target_type)
    is_tgt_union = tgt_origin is type(int | str) or (
        hasattr(pytypes, "UnionType") and isinstance(target_type, pytypes.UnionType)
    )
    if is_tgt_union:
        return any(_types_compatible(source_type, a) for a in get_args(target_type))
    # Generic containers — match on origin (list[int] vs list[str] both list)
    if src_origin is not None and tgt_origin is not None:
        return src_origin is tgt_origin
    if src_origin is not None:
        try:
            return issubclass(src_origin, target_type)
        except TypeError:
            return False
    # Plain class inheritance
    try:
        return issubclass(source_type, target_type)
    except TypeError:
        return False


def _check_roundtrip(
    f: Callable[..., Any],
    g: Callable[..., Any],
    fname: str,
    gname: str,
    *,
    max_examples: int = 30,
) -> CrossFunctionProperty | None:
    """Test whether ``g(f(x)) == x`` — the roundtrip property.

    Only attempted when f's return type is compatible with g's first
    parameter type.  Returns ``None`` if the pair is not type-compatible
    or if no examples could be generated.
    """
    ret_f = _return_type(f)
    g_param_name, g_param_type = _first_param_type(g)
    if not _types_compatible(ret_f, g_param_type) or g_param_name is None:
        return None

    strategies = _infer_strategies(f)
    if strategies is None:
        return None

    f_param_name, _f_param_type = _first_param_type(f)
    if f_param_name is None:
        return None

    inputs: list[dict[str, Any]] = []
    outputs_f: list[Any] = []
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            out = f(**kwargs)
            outputs_f.append(out)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    if not inputs:
        return None

    holds = total = 0
    counterexample: dict[str, Any] | None = None
    cap = min(len(inputs), max_examples)
    for kwargs, out_f in zip(inputs[:cap], outputs_f[:cap]):
        if out_f is None:
            continue
        try:
            back = g(**{g_param_name: out_f})
            total += 1
            if _approx_equal(back, kwargs[f_param_name]):
                holds += 1
            elif counterexample is None:
                counterexample = {
                    "input": kwargs[f_param_name],
                    f"{fname}_output": out_f,
                    f"{gname}_output": back,
                }
        except Exception:
            pass

    if total == 0:
        return None

    return CrossFunctionProperty(
        function_a=fname,
        function_b=gname,
        relation="roundtrip",
        confidence=holds / total,
        holds=holds,
        total=total,
        counterexample=counterexample,
    )


def _check_composition_commutativity(
    f: Callable[..., Any],
    g: Callable[..., Any],
    fname: str,
    gname: str,
    *,
    max_examples: int = 30,
) -> CrossFunctionProperty | None:
    """Test whether ``f(g(x)) == g(f(x))`` — commutative composition.

    Only attempted when both functions accept the same first-parameter
    type and each function's return type is compatible with the other's
    input.  Returns ``None`` if the pair is not type-compatible or if
    no examples could be generated.
    """
    f_param_name, f_param_type = _first_param_type(f)
    g_param_name, g_param_type = _first_param_type(g)
    ret_f = _return_type(f)
    ret_g = _return_type(g)

    # Both must accept the same type, and each output must feed into the other
    if not _types_compatible(f_param_type, g_param_type):
        return None
    if not _types_compatible(ret_f, g_param_type):
        return None
    if not _types_compatible(ret_g, f_param_type):
        return None
    if f_param_name is None or g_param_name is None:
        return None

    strategies = _infer_strategies(f)
    if strategies is None:
        return None

    inputs: list[dict[str, Any]] = []
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    if not inputs:
        return None

    holds = total = 0
    counterexample: dict[str, Any] | None = None
    cap = min(len(inputs), max_examples)
    for kwargs in inputs[:cap]:
        x = kwargs[f_param_name]
        try:
            fx = f(**{f_param_name: x})
            gx = g(**{g_param_name: x})
            g_of_fx = g(**{g_param_name: fx})  # g(f(x))
            f_of_gx = f(**{f_param_name: gx})  # f(g(x))
            total += 1
            if _approx_equal(g_of_fx, f_of_gx):
                holds += 1
            elif counterexample is None:
                counterexample = {
                    "input": x,
                    f"g({fname}(x))": g_of_fx,
                    f"f({gname}(x))": f_of_gx,
                }
        except Exception:
            pass

    if total == 0:
        return None

    return CrossFunctionProperty(
        function_a=fname,
        function_b=gname,
        relation="commutative_composition",
        confidence=holds / total,
        holds=holds,
        total=total,
        counterexample=counterexample,
    )


def _check_output_equivalence(
    f: Callable[..., Any],
    g: Callable[..., Any],
    fname: str,
    gname: str,
    *,
    max_examples: int = 30,
) -> CrossFunctionProperty | None:
    """Test whether ``f(x) == g(x)`` — output equivalence.

    Only attempted when both functions accept the same parameter types.
    Detects duplicate implementations, reference/optimized pairs, or
    accidental copies.  Returns ``None`` if the pair is not
    type-compatible or if no examples could be generated.
    """
    f_param_name, f_param_type = _first_param_type(f)
    g_param_name, g_param_type = _first_param_type(g)

    if not _types_compatible(f_param_type, g_param_type):
        return None
    if f_param_name is None or g_param_name is None:
        return None

    strategies = _infer_strategies(f)
    if strategies is None:
        return None

    inputs: list[dict[str, Any]] = []
    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass

    if not inputs:
        return None

    holds = total = 0
    counterexample: dict[str, Any] | None = None
    cap = min(len(inputs), max_examples)
    for kwargs in inputs[:cap]:
        x = kwargs[f_param_name]
        try:
            out_f = f(**{f_param_name: x})
            out_g = g(**{g_param_name: x})
            total += 1
            if _approx_equal(out_f, out_g):
                holds += 1
            elif counterexample is None:
                counterexample = {
                    "input": x,
                    f"{fname}_output": out_f,
                    f"{gname}_output": out_g,
                }
        except Exception:
            pass

    if total == 0:
        return None

    return CrossFunctionProperty(
        function_a=fname,
        function_b=gname,
        relation="equivalent",
        confidence=holds / total,
        holds=holds,
        total=total,
        counterexample=counterexample,
    )


def mine_module(
    module: str | ModuleType,
    *,
    max_examples: int = 200,
    cross_max_examples: int = 30,
    mine_per_function: bool = True,
    **fixtures: st.SearchStrategy[Any] | Any,
) -> MineModuleResult:
    """Discover properties across an entire module — both per-function and cross-function.

    Single-function mining (via ``mine()``) finds properties like "output >= 0"
    or "deterministic".  Cross-function mining finds relationships that only
    exist *between* functions — the kind of properties that break during
    refactoring because no single unit test covers the contract.

    Three cross-function relationships are checked for every compatible pair:

    - **roundtrip**: ``g(f(x)) == x``.  Discovered when f's return type matches
      g's parameter type.  Classic examples: ``decode(encode(x))``,
      ``deserialize(serialize(x))``, ``decompress(compress(x))``.

    - **commutative_composition**: ``f(g(x)) == g(f(x))``.  Discovered when
      both functions accept and return the same type.  Examples: two
      normalization passes that can be applied in either order, or
      ``sort(reverse(xs)) == reverse(sort(xs))`` (which would *not* hold
      and produce a counterexample).

    - **equivalent**: ``f(x) == g(x)``.  Discovered when both functions
      accept the same input types.  Flags duplicate implementations,
      reference/optimized pairs, or accidental copies that should be
      consolidated.

    Because the number of pairs grows as O(n^2), ``cross_max_examples`` is
    kept low (default 30) to avoid combinatorial blowup.  For a module with
    10 functions there are 45 directed pairs; at 30 examples each that is
    1350 calls per relationship check — fast enough for CI.

    Args:
        module: Dotted module path (``"myapp.scoring"``) or an already-imported
            module object.
        max_examples: Examples per function for individual ``mine()`` calls.
        cross_max_examples: Examples per function pair for cross-function checks.
            Kept low because there are O(n^2) pairs.
        mine_per_function: If ``True`` (default), also run ``mine()`` on each
            function individually.  Set to ``False`` to only discover
            cross-function relationships.
        **fixtures: Strategy overrides or plain values passed through to
            ``mine()`` and ``_infer_strategies()``.

    Returns:
        A ``MineModuleResult`` containing per-function ``MineResult`` objects
        and a list of ``CrossFunctionProperty`` relationships.

    Example::

        result = mine_module("myapp.codecs")
        print(result.summary())
        # mine_module(myapp.codecs)
        #   4 functions, 2 cross-function relationships
        #
        #   mine(encode): 200 examples
        #     ALWAYS  output type is bytes (200/200)
        #     ...
        #
        #   Cross-function relationships:
        #     ALWAYS  encode <-> decode: roundtrip (30/30)
        #      97%    fast_encode <-> encode: equivalent (29/30)
    """
    if isinstance(module, str):
        mod = importlib.import_module(module)
        mod_name = module
    else:
        mod = module
        mod_name = getattr(mod, "__name__", str(mod))

    funcs = _get_public_functions(mod)

    # --- Per-function mining ---
    per_function: dict[str, MineResult] = {}
    if mine_per_function:
        for name, fn in funcs:
            try:
                per_function[name] = mine(fn, max_examples=max_examples, **fixtures)
            except (ValueError, TypeError):
                pass  # can't infer strategies — skip

    # --- Cross-function mining ---
    # Build a lookup of functions with their signatures resolved
    typed_funcs: list[tuple[str, Callable[..., Any]]] = []
    for name, fn in funcs:
        # Only include functions where we can infer at least the first param
        param_name, _param_type = _first_param_type(fn)
        if param_name is not None:
            typed_funcs.append((name, fn))

    cross_function: list[CrossFunctionProperty] = []

    for i, (fname, f) in enumerate(typed_funcs):
        for j, (gname, g) in enumerate(typed_funcs):
            if i == j:
                continue

            # Roundtrip: g(f(x)) == x — directed, so check both (i,j) and (j,i)
            # Only check (i,j) direction here; (j,i) is checked when i/j swap
            if i < j:
                prop = _check_roundtrip(f, g, fname, gname, max_examples=cross_max_examples)
                if prop is not None and prop.total > 0:
                    cross_function.append(prop)

                prop = _check_roundtrip(g, f, gname, fname, max_examples=cross_max_examples)
                if prop is not None and prop.total > 0:
                    cross_function.append(prop)

            # Commutative composition: f(g(x)) == g(f(x)) — symmetric, check once
            if i < j:
                prop = _check_composition_commutativity(
                    f, g, fname, gname, max_examples=cross_max_examples
                )
                if prop is not None and prop.total > 0:
                    cross_function.append(prop)

            # Output equivalence: f(x) == g(x) — symmetric, check once
            if i < j:
                prop = _check_output_equivalence(
                    f, g, fname, gname, max_examples=cross_max_examples
                )
                if prop is not None and prop.total > 0:
                    cross_function.append(prop)

    return MineModuleResult(
        module=mod_name,
        per_function=per_function,
        cross_function=cross_function,
    )
