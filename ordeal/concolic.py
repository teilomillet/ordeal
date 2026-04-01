"""Concolic execution — crack branches that random testing and CMPLOG cannot reach.

CrossHair is a Python-specific concolic execution engine.  Where random testing
generates inputs blindly and CMPLOG reads source to extract literal comparison
values, concolic execution *executes the function symbolically* — tracking path
constraints through every branch, then solving those constraints with an SMT
solver to generate inputs that reach uncovered paths.

When it helps:

- **Saturated coverage**: mine() has explored many inputs but found no new
  edges for the last 50%+ of examples.  Random testing hit a wall.
- **Guarded branches CMPLOG missed**: CMPLOG extracts literal values from
  source (``if x == 42``), but cannot handle computed guards like
  ``if hash(x) % 7 == 3`` or ``if len(s) == sum(ord(c) for c in t)``.
  Concolic execution reasons about these symbolically.
- **Tight input constraints**: Functions with narrow valid input ranges
  (e.g., ``if 3.14 < x < 3.15 and y == x**2``) that random generation
  almost never satisfies.

When it does not help:

- **Complex data structures**: CrossHair works best on functions with
  primitive inputs (int, float, str, bool).  Deeply nested dicts, custom
  classes, or opaque objects resist symbolic reasoning.
- **External calls**: I/O, network, database calls, or C extensions are
  opaque to the symbolic engine — it cannot reason past them.
- **Side effects**: Functions that depend on global mutable state or
  randomness produce path constraints the solver cannot capture.
- **Large functions**: Very long functions with many branches may exhaust
  the solver's time budget before covering interesting paths.

How it complements CMPLOG:

- CMPLOG reads source *statically* — it extracts values from AST comparison
  nodes without executing the function.  Fast, cheap, always available.
- Concolic execution runs the function *symbolically* — it tracks every
  operation on symbolic inputs through the interpreter, building path
  constraints for each branch.  Slower, deeper, requires CrossHair.
- Together: CMPLOG cracks simple guards (``== 42``, ``in ["a", "b"]``),
  concolic cracks computed guards and tight numerical constraints.

Install::

    pip install crosshair-tool

Usage::

    from ordeal.concolic import crack_branches, enhance_mine_with_concolic
    from ordeal.mine import mine

    # Standalone: find inputs that reach different code paths
    inputs = crack_branches(my_function, max_seconds=10)
    for kwargs in inputs:
        print(kwargs)

    # Integrated: enhance a saturated mine result
    result = mine(my_function)
    if result.saturated:
        enhanced = enhance_mine_with_concolic(my_function, result)
        print(enhanced.summary())
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)

_CROSSHAIR_AVAILABLE: bool | None = None
_INSTALL_HINT = "CrossHair is not installed. Install with: pip install crosshair-tool"


def _check_crosshair() -> bool:
    """Check whether CrossHair is importable, caching the result."""
    global _CROSSHAIR_AVAILABLE
    if _CROSSHAIR_AVAILABLE is None:
        try:
            import crosshair  # noqa: F401

            _CROSSHAIR_AVAILABLE = True
        except ImportError:
            _CROSSHAIR_AVAILABLE = False
    return _CROSSHAIR_AVAILABLE


def crack_branches(
    fn: Callable[..., Any],
    *,
    max_seconds: int = 10,
) -> list[dict[str, Any]]:
    """Use concolic execution to find inputs that cover different code paths.

    Runs CrossHair's symbolic analysis on *fn* to discover inputs that
    satisfy different branch conditions.  Each returned dict is a set of
    kwargs that reaches a distinct code path.

    Args:
        fn: The function to analyze.  Works best with primitive-typed
            parameters (int, float, str, bool).
        max_seconds: Time budget for the solver in seconds.

    Returns:
        A list of kwarg dicts, each reaching a different branch.
        Empty list if CrossHair is not installed or analysis finds nothing.

    Example::

        def categorize(x: int) -> str:
            if x < 0:
                return "negative"
            elif x == 0:
                return "zero"
            else:
                return "positive"

        inputs = crack_branches(categorize, max_seconds=5)
        # [{"x": -1}, {"x": 0}, {"x": 1}]  (or similar)
    """
    if not _check_crosshair():
        logger.info(_INSTALL_HINT)
        return []

    # Unwrap decorated functions so we analyze the real code
    fn = getattr(fn, "_function", fn)
    try:
        fn = inspect.unwrap(fn)
    except (ValueError, TypeError):
        pass

    try:
        from crosshair.core_and_libs import (
            MessageType,
            analyze_function,
        )
        from crosshair.options import AnalysisOptionSet
    except ImportError:
        logger.info(_INSTALL_HINT)
        return []

    results: list[dict[str, Any]] = []
    seen_outputs: set[str] = set()

    try:
        options = AnalysisOptionSet(
            max_iterations=max_seconds * 10,
            per_condition_timeout=max_seconds,
            per_path_timeout=max(1, max_seconds // 3),
        )

        for msg in analyze_function(fn, options):
            if msg.state == MessageType.CONFIRMED:
                continue
            # Extract the example arguments from the message
            if hasattr(msg, "args") and msg.args:
                kwargs = _extract_kwargs(fn, msg.args)
                if kwargs is not None:
                    # Deduplicate by output to get diverse path coverage
                    key = repr(sorted(kwargs.items()))
                    if key not in seen_outputs:
                        seen_outputs.add(key)
                        results.append(kwargs)
    except Exception:
        # CrossHair can fail on functions it cannot analyze —
        # complex types, C extensions, etc.  That's expected.
        logger.debug("CrossHair analysis failed for %s", getattr(fn, "__name__", fn))

    return results


def _extract_kwargs(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
) -> dict[str, Any] | None:
    """Map positional args from CrossHair back to keyword arguments."""
    try:
        sig = inspect.signature(fn)
        params = [
            name
            for name, p in sig.parameters.items()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY, p.KEYWORD_ONLY)
            and name not in ("self", "cls")
        ]
        if len(args) > len(params):
            return None
        return dict(zip(params, args))
    except (ValueError, TypeError):
        return None


def enhance_mine_with_concolic(
    fn: Callable[..., Any],
    mine_result: Any,
    *,
    max_seconds: int = 10,
) -> Any:
    """Enhance a saturated MineResult with concolic-discovered inputs.

    When mine() reports ``saturated=True``, random testing and mutation-based
    exploration have exhausted their ability to find new edges.  This function
    uses CrossHair's concolic execution to reason symbolically about path
    constraints and generate inputs that reach branches random testing missed.

    The concolic inputs are:

    1. Run through *fn* to collect their outputs.
    2. Checked against existing properties (do they still hold?).
    3. Merged into the MineResult's collected_inputs and collected_outputs.

    Args:
        fn: The function that was mined.
        mine_result: A ``MineResult`` from ``mine()``.
        max_seconds: Time budget for CrossHair in seconds.

    Returns:
        An enhanced ``MineResult`` with additional examples from concolic
        execution.  If CrossHair is not installed, or the mine result is
        not saturated, returns the original result unchanged.

    Example::

        from ordeal.mine import mine
        from ordeal.concolic import enhance_mine_with_concolic

        result = mine(guarded_function, max_examples=500)
        if result.saturated:
            result = enhance_mine_with_concolic(guarded_function, result)
            # result now includes concolic-discovered inputs
    """
    if not mine_result.saturated:
        return mine_result

    concolic_inputs = crack_branches(fn, max_seconds=max_seconds)
    if not concolic_inputs:
        return mine_result

    # Unwrap for execution
    unwrapped = getattr(fn, "_function", fn)
    try:
        unwrapped = inspect.unwrap(unwrapped)
    except (ValueError, TypeError):
        pass

    # Run concolic inputs through the function, collecting outputs
    new_inputs: list[dict[str, object]] = []
    new_outputs: list[object] = []
    concolic_cracked = 0

    # Optionally collect edge coverage if available
    collector = None
    edges_seen: set[int] = set()
    try:
        from ordeal.explore import CoverageCollector

        collector = CoverageCollector()
        # Seed with edges already known from the mine result
        # (we don't have the raw edge set, but we know how many were found)
    except ImportError:
        pass

    for kwargs in concolic_inputs:
        try:
            if collector:
                collector.start()
            result = unwrapped(**kwargs)
            if collector:
                edges = collector.stop()
                new_edges = edges - edges_seen
                if new_edges:
                    edges_seen.update(new_edges)
                    concolic_cracked += 1
            new_inputs.append(kwargs)
            new_outputs.append(result)
        except Exception:
            if collector:
                try:
                    collector.stop()
                except Exception:
                    pass
            # Concolic may generate inputs that crash — that's fine,
            # we still learned about the path even if it raises.
            continue

    if not new_inputs:
        return mine_result

    # Merge into the existing result
    merged_inputs = list(mine_result.collected_inputs) + new_inputs
    merged_outputs = list(mine_result.collected_outputs) + new_outputs

    # Re-check properties with the extended set to see if any broke
    from ordeal.mine import (
        MinedProperty,
        _check_bounded_01,
        _check_deterministic,
        _check_never_empty,
        _check_never_none,
        _check_no_nan,
        _check_non_negative,
        _check_type_consistent,
    )

    new_props: list[MinedProperty] = [
        _check_type_consistent(merged_outputs),
        _check_never_none(merged_outputs),
        _check_no_nan(merged_outputs),
        _check_non_negative(merged_outputs),
        _check_bounded_01(merged_outputs),
        _check_never_empty(merged_outputs),
        _check_deterministic(unwrapped, merged_inputs),
    ]
    props = [p for p in new_props if p.total > 0]

    # Preserve properties from the original that we didn't re-check
    rechecked_names = {p.name for p in new_props}
    for p in mine_result.properties:
        if p.name not in rechecked_names:
            props.append(p)

    return replace(
        mine_result,
        examples=len(merged_outputs),
        properties=props,
        collected_inputs=merged_inputs,
        collected_outputs=merged_outputs,
        edges_discovered=mine_result.edges_discovered + len(edges_seen),
        branches_cracked=mine_result.branches_cracked + concolic_cracked,
    )
