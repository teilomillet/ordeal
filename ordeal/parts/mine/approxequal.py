from __future__ import annotations
# ruff: noqa
import importlib
import inspect
import math
import random as _random
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, get_args, get_origin
import hypothesis.strategies as st
from hypothesis import find, given, settings
from ordeal.auto import _call_sync, _get_public_functions, _infer_strategies
from ordeal.introspection import safe_get_annotations
_REL_TOL = 1e-9
_ABS_TOL = 1e-12
_SUSPICIOUS_CONFIDENCE = 0.8
_MAX_SUSPICIOUS_PROPERTIES = 4
_NOT_CHECKED_PREVIEW = 3
_SUMMARY_OMIT_PREFIXES = ("observed range",)
_SUSPICIOUS_PREFIXES = (
    "never None",
    "no NaN",
    "deterministic",
    "idempotent",
    "involution",
    "commutative",
    "associative",
    "bijective",
)
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
def _short_repr(value: Any, *, limit: int = 40) -> str:
    """Return a compact repr suitable for one-line summaries."""
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
def _show_in_summary(name: str) -> bool:
    """Return True when a property should appear in the default summary."""
    return not name.startswith(_SUMMARY_OMIT_PREFIXES)
def _normalize_property_token(name: str) -> str:
    """Normalize property/relation names for config-driven suppression."""
    import re

    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
def _suppressed_names(names: list[str] | tuple[str, ...] | None) -> set[str]:
    """Normalize suppression names into a lookup set."""
    return {_normalize_property_token(name) for name in (names or []) if name}
def _is_suspicious_property(prop: "MinedProperty") -> bool:
    """Return True for partial properties worth surfacing by default."""
    if prop.universal or prop.total < 10 or prop.confidence < _SUSPICIOUS_CONFIDENCE:
        return False
    if not _show_in_summary(prop.name):
        return False
    return prop.name.startswith(_SUSPICIOUS_PREFIXES)
def _property_sort_key(prop: "MinedProperty") -> tuple[float, int, str]:
    """Sort stronger, higher-signal properties first."""
    has_counterexample = 0 if prop.counterexample else 1
    return (-prop.confidence, has_counterexample, prop.name)
def _format_counterexample(counterexample: dict[str, Any] | None) -> str | None:
    """Render a compact counterexample line for suspicious findings."""
    if not counterexample:
        return None

    preferred = [
        ("input", "input"),
        ("output", "output"),
        ("value", "value"),
        ("first", "first"),
        ("second", "second"),
        ("replayed", "replayed"),
        ("expected", "expected"),
        ("got", "got"),
    ]
    parts: list[str] = []
    for key, label in preferred:
        if key in counterexample:
            parts.append(f"{label}={_short_repr(counterexample[key])}")

    if not parts:
        for key, value in counterexample.items():
            if key == "index":
                continue
            parts.append(f"{key}={_short_repr(value)}")
            if len(parts) >= 3:
                break

    if not parts:
        return None
    return ", ".join(parts[:3])
def _format_not_checked(not_checked: list[str]) -> str:
    """Render a short reminder of mine()'s structural blind spots."""
    if not not_checked:
        return ""
    preview = ", ".join(not_checked[:_NOT_CHECKED_PREVIEW])
    remaining = len(not_checked) - _NOT_CHECKED_PREVIEW
    if remaining > 0:
        preview += f", +{remaining} more"
    return preview
@dataclass
class MinedProperty:
    """A likely property observed during mining."""

    name: str
    holds: int
    total: int
    counterexample: dict[str, Any] | None = None
    replayable: bool | None = None
    replay_attempts: int = 0
    replay_matches: int = 0
    replay_match_basis: str | None = None
    minimization: dict[str, Any] | None = None

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
_DURABLE_PROPERTY_NAMES = {
    "never None",
    "no NaN",
    "deterministic",
    "idempotent",
    "involution",
    "commutative",
    "associative",
    "bijective",
}
def _contains_nan(value: Any) -> bool:
    """Return whether a scalar or array-like value contains NaN."""
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, (list, tuple)):
        return any(_contains_nan(item) for item in value)
    if hasattr(value, "shape"):
        try:
            import numpy as np

            return bool(np.isnan(np.asarray(value)).any())
        except (ImportError, TypeError, ValueError):
            return False
    return False
def _property_counterexample(
    fn: Callable[..., Any],
    name: str,
    kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """Re-evaluate one inferred property on an exact input witness."""
    try:
        if name == "bijective":
            first_args = kwargs.get("__first__")
            second_args = kwargs.get("__second__")
            if not isinstance(first_args, dict) or not isinstance(second_args, dict):
                return None
            if first_args == second_args:
                return None
            first_output = _call_sync(fn, **first_args)
            second_output = _call_sync(fn, **second_args)
            if _approx_equal(first_output, second_output):
                return {
                    "output": first_output,
                    "colliding_inputs": [
                        tuple(sorted(first_args.items())),
                        tuple(sorted(second_args.items())),
                    ],
                }
            return None
        if name == "never None":
            output = _call_sync(fn, **kwargs)
            return {"input": kwargs, "output": output} if output is None else None
        if name == "no NaN":
            output = _call_sync(fn, **kwargs)
            return {"input": kwargs, "value": output} if _contains_nan(output) else None
        if name == "deterministic":
            first = _call_sync(fn, **kwargs)
            second = _call_sync(fn, **kwargs)
            if not _approx_equal(first, second):
                return {"input": kwargs, "first": first, "second": second}
            return None

        params = [
            param for param in inspect.signature(fn).parameters if param not in ("self", "cls")
        ]
        if not params:
            return None
        first_param = params[0]
        if name in {"idempotent", "involution"}:
            first = _call_sync(fn, **kwargs)
            replay_args = dict(kwargs)
            replay_args[first_param] = first
            second = _call_sync(fn, **replay_args)
            expected = first if name == "idempotent" else kwargs[first_param]
            if not _approx_equal(second, expected):
                return {"input": kwargs, "output": first, "replayed": second}
            return None
        if name == "commutative" and len(params) == 2:
            left_name, right_name = params
            swapped = {
                left_name: kwargs[right_name],
                right_name: kwargs[left_name],
            }
            left = _call_sync(fn, **kwargs)
            right = _call_sync(fn, **swapped)
            if not _approx_equal(left, right):
                return {
                    "input": kwargs,
                    "output": left,
                    "swapped_input": swapped,
                    "swapped_output": right,
                }
        if name == "associative" and len(params) == 2 and "third" in kwargs:
            left_name, right_name = params
            first_value = kwargs[left_name]
            second_value = kwargs[right_name]
            third_value = kwargs["third"]
            middle_right = _call_sync(
                fn,
                **{left_name: second_value, right_name: third_value},
            )
            left = _call_sync(
                fn,
                **{left_name: first_value, right_name: middle_right},
            )
            middle_left = _call_sync(
                fn,
                **{left_name: first_value, right_name: second_value},
            )
            right = _call_sync(
                fn,
                **{left_name: middle_left, right_name: third_value},
            )
            if not _approx_equal(left, right):
                return {"input": kwargs, "left": left, "right": right}
    except Exception:
        return None
    return None
def _witness_complexity(value: Any) -> int:
    """Return a stable, transparent size proxy for witness comparison."""
    import json

    return len(json.dumps(value, sort_keys=True, default=repr, separators=(",", ":")))
def _minimize_and_replay_property(
    fn: Callable[..., Any],
    prop: MinedProperty,
    strategies: dict[str, st.SearchStrategy[Any]],
    *,
    max_examples: int,
) -> None:
    """Shrink and immediately replay a concrete inferred-property witness.

    Hypothesis minimizes within the declared input strategies. This is not a
    proof that the witness is globally smallest, so that boundary is carried
    in the evidence metadata.
    """
    if prop.name not in _DURABLE_PROPERTY_NAMES or not prop.counterexample:
        return
    original_input = prop.counterexample.get("input")
    case_strategy: st.SearchStrategy[Any] = st.fixed_dictionaries(strategies)
    if prop.name == "associative":
        params = [
            param for param in inspect.signature(fn).parameters if param not in ("self", "cls")
        ]
        if len(params) != 2 or not isinstance(original_input, dict):
            return
        first_param = params[0]
        case_strategy = st.tuples(case_strategy, strategies[first_param]).map(
            lambda pair: {**pair[0], "third": pair[1]}
        )
    elif prop.name == "bijective":
        colliding = prop.counterexample.get("colliding_inputs")
        if not isinstance(colliding, (list, tuple)) or len(colliding) < 2:
            return
        try:
            original_input = {
                "__first__": dict(colliding[0]),
                "__second__": dict(colliding[1]),
            }
        except (TypeError, ValueError):
            return
        base_strategy = case_strategy
        case_strategy = st.tuples(base_strategy, base_strategy).map(
            lambda pair: {"__first__": pair[0], "__second__": pair[1]}
        )
    if not isinstance(original_input, dict):
        return

    minimized_input = dict(original_input)
    status = "not_run"
    try:
        minimized_input = find(
            case_strategy,
            lambda kwargs: _property_counterexample(fn, prop.name, dict(kwargs)) is not None,
            settings=settings(
                max_examples=max(50, min(500, max_examples * 4)),
                database=None,
                derandomize=True,
                deadline=None,
            ),
        )
        status = "performed"
    except Exception:
        # The originally observed witness remains useful evidence. A failed
        # minimization attempt must not be mislabeled as a minimized witness.
        minimized_input = dict(original_input)

    observed = _property_counterexample(fn, prop.name, dict(minimized_input))
    if observed is not None:
        prop.counterexample = observed

    attempts = 2
    matches = sum(
        _property_counterexample(fn, prop.name, dict(minimized_input)) is not None
        for _ in range(attempts)
    )
    prop.replayable = matches == attempts
    prop.replay_attempts = attempts
    prop.replay_matches = matches
    prop.replay_match_basis = "same inferred property violated on the same input"
    prop.minimization = {
        "status": "verified" if status == "performed" and prop.replayable else status,
        "method": "hypothesis.find" if status == "performed" else None,
        "original_complexity": _witness_complexity(original_input),
        "minimized_complexity": _witness_complexity(minimized_input),
        "replay_attempts": attempts,
        "replay_matches": matches,
        "boundary": (
            "Shrunk within the declared Hypothesis strategies; this does not prove "
            "global minimality."
            if status == "performed"
            else "No smaller witness was established; the observed witness was replayed as-is."
        ),
    }
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
        universal = sorted(
            [p for p in self.properties if p.universal and _show_in_summary(p.name)],
            key=_property_sort_key,
        )
        suspicious = sorted(
            [p for p in self.properties if _is_suspicious_property(p)],
            key=_property_sort_key,
        )[:_MAX_SUSPICIOUS_PROPERTIES]

        for prop in universal:
            lines.append(str(prop))

        if suspicious:
            lines.append("  suspicious findings:")
            for prop in suspicious:
                lines.append(str(prop))
                ce = _format_counterexample(prop.counterexample)
                if ce:
                    lines.append(f"           counterexample: {ce}")

        not_checked = _format_not_checked(self.not_checked)
        if not_checked:
            lines.append(f"  not checked: {not_checked}")
        from ordeal.suggest import format_suggestions

        avail = format_suggestions(self)
        if avail:
            lines.append(avail)
        return "\n".join(lines)
