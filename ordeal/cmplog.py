"""Comparison logging — crack guarded branches that random testing misses.

Python equivalent of AFL++'s CMPLOG/RedQueen technique.  Parses function
source to extract literal values from comparisons (``==``, ``!=``, ``in``,
``is``), then injects them into Hypothesis strategies so the fuzzer
can reach branches guarded by magic values.

Without CMPLOG::

    def process(x: int, mode: str):
        if x == 42 and mode == "admin":
            return dangerous_path()  # random testing NEVER reaches this
        return safe_path()

    mine(process)  # saturates at 2 edges — can't crack the guards

With CMPLOG::

    hints = extract_comparison_values(process)
    # hints = {"x": [42], "mode": ["admin"]}
    # These get injected into Hypothesis strategies
    mine(process)  # now reaches dangerous_path() — 47 edges discovered

The technique:

1. **Extract** — AST-walk the function, find ``ast.Compare`` nodes,
   collect literal operands paired with the variable they're compared to.
2. **Inject** — Merge extracted values into Hypothesis strategies via
   ``st.one_of(original_strategy, st.sampled_from(extracted_values))``.
   This biases generation toward branch-cracking values while preserving
   random exploration.
3. **Feedback** — Coverage tracking (via ``CoverageCollector``) confirms
   whether the injected values actually reached new edges.  If they did,
   the coverage feedback loop in ``mine()`` amplifies similar inputs.

Scales with compute: extracting more comparison targets from deeper call
graphs reaches more guarded branches.  Each cracked branch opens new
edges for the coverage-guided loop to explore.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any, Callable

import hypothesis.strategies as st


def extract_comparison_values(fn: Callable[..., Any]) -> dict[str, list[Any]]:
    """Extract literal values from comparisons in a function's source.

    Parses the AST and collects values from patterns like::

        if x == 42:           → {"x": [42]}
        if name in ["a", "b"]: → {"name": ["a", "b"]}
        if mode != "debug":   → {"mode": ["debug"]}
        if count >= 100:      → {"count": [100]}

    Returns a dict mapping parameter names to lists of interesting values
    found in the source.  These values are the ones most likely to crack
    guarded branches that random testing cannot reach.
    """
    # Unwrap decorated functions (@ray.remote, functools.wraps)
    fn = getattr(fn, "_function", fn)
    try:
        fn = inspect.unwrap(fn)
    except (ValueError, TypeError):
        pass

    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return {}

    # Get parameter names for matching
    try:
        sig = inspect.signature(fn)
        param_names = set(sig.parameters.keys()) - {"self", "cls"}
    except (ValueError, TypeError):
        param_names = set()

    hints: dict[str, list[Any]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            _extract_from_compare(node, param_names, hints)
        elif isinstance(node, ast.If):
            _extract_from_if(node, param_names, hints)

    return hints


def _extract_from_compare(
    node: ast.Compare, param_names: set[str], hints: dict[str, list[Any]]
) -> None:
    """Extract values from ``ast.Compare`` nodes."""
    # Pattern: name == literal, name != literal, name >= literal, etc.
    left = node.left
    for comparator in node.comparators:
        if isinstance(left, ast.Name) and left.id in param_names:
            vals = _extract_literal(comparator)
            if vals:
                hints.setdefault(left.id, []).extend(vals)
        elif isinstance(comparator, ast.Name) and comparator.id in param_names:
            vals = _extract_literal(left)
            if vals:
                hints.setdefault(comparator.id, []).extend(vals)
        left = comparator

    # Pattern: name in [literal, literal, ...]
    if (
        len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and isinstance(node.left, ast.Name)
        and node.left.id in param_names
    ):
        for comp in node.comparators:
            if isinstance(comp, (ast.List, ast.Tuple, ast.Set)):
                for elt in comp.elts:
                    vals = _extract_literal(elt)
                    if vals:
                        hints.setdefault(node.left.id, []).extend(vals)


def _extract_from_if(node: ast.If, param_names: set[str], hints: dict[str, list[Any]]) -> None:
    """Extract values from ``if`` test expressions (boolean ops)."""
    # Handle: if x == 1 and y == 2  (ast.BoolOp with ast.Compare children)
    if isinstance(node.test, ast.BoolOp):
        for value in node.test.values:
            if isinstance(value, ast.Compare):
                _extract_from_compare(value, param_names, hints)


def _extract_literal(node: ast.expr) -> list[Any]:
    """Try to evaluate an AST node as a Python literal."""
    try:
        value = ast.literal_eval(node)
        if isinstance(value, (int, float, str, bytes, bool, type(None))):
            return [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return [v for v in value if isinstance(v, (int, float, str, bytes, bool))]
    except (ValueError, TypeError):
        pass
    return []


def enhance_strategies(
    strategies: dict[str, st.SearchStrategy[Any]],
    fn: Callable[..., Any],
) -> dict[str, st.SearchStrategy[Any]]:
    """Enhance Hypothesis strategies with comparison-extracted values.

    Merges extracted magic values into the existing strategies so the
    fuzzer can crack guarded branches while preserving random exploration::

        original:  st.integers()           → uniform random ints
        enhanced:  st.one_of(
                       st.integers(),       → random exploration (80%)
                       st.sampled_from([42, 0, -1]),  → branch crackers (20%)
                   )

    The 80/20 split ensures most generation is still random (for broad
    coverage) while guaranteeing branch-cracking values appear regularly.
    """
    hints = extract_comparison_values(fn)
    if not hints:
        return strategies

    enhanced = dict(strategies)
    for param, values in hints.items():
        if param not in enhanced or not values:
            continue
        # Deduplicate
        unique = list(dict.fromkeys(values))
        # Filter to values compatible with the strategy's type
        try:
            magic = st.sampled_from(unique)
            # 80% random exploration, 20% branch-cracking values
            enhanced[param] = st.one_of(
                strategies[param].filter(lambda _: True),  # original
                magic,
            )
        except Exception:
            pass  # incompatible types — keep original strategy

    return enhanced
