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

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import hypothesis.strategies as st
from hypothesis import given, settings

from ordeal.auto import _infer_strategies

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
    return a == b


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
        lines = [f"mine({self.function}): {self.examples} examples"]
        for p in sorted(self.properties, key=lambda p: -p.confidence):
            if p.confidence >= 0.5:
                lines.append(str(p))
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

    # Collect outputs and inputs
    outputs: list[Any] = []
    inputs: list[dict[str, Any]] = []

    try:

        @given(**strategies)
        @settings(max_examples=max_examples, database=None)
        def collect(**kwargs: Any) -> None:
            result = fn(**kwargs)
            outputs.append(result)
            inputs.append(dict(kwargs))

        collect()
    except Exception:
        pass  # some inputs may crash — that's fine, we analyze what we got

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
    ]
    all_props.extend(_check_monotonic(inputs, outputs))
    all_props.extend(_check_length_relationship(inputs, outputs))
    all_props.append(_check_commutative(fn, inputs, outputs))
    all_props.append(_check_associative(fn, inputs))

    # Separate applicable (total > 0) from not-applicable (total == 0)
    props = [p for p in all_props if p.total > 0]
    not_applicable = [p.name for p in all_props if p.total == 0]

    name = getattr(fn, "__name__", str(fn))
    return MineResult(
        function=name,
        examples=len(outputs),
        properties=props,
        not_applicable=not_applicable,
        collected_inputs=inputs,
        collected_outputs=outputs,
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
