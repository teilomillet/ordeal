"""Named, composable invariant assertions.

Invariants are reusable checks with clear failure messages.
They compose with ``&``::

    from ordeal.invariants import no_nan, no_inf, bounded

    valid_score = no_nan & no_inf & bounded(0, 1)
    valid_score(model_output)          # raises AssertionError on violation
    valid_score(model_output, name="final_score")  # custom name in message
"""

from __future__ import annotations

import math
from typing import Any, Callable


class Invariant:
    """A named, composable assertion."""

    def __init__(self, name: str, check_fn: Callable[..., None]):
        self.name = name
        self._check = check_fn

    def __call__(self, value: Any, *, name: str | None = None) -> None:
        """Run the invariant check, raising ``AssertionError`` on violation."""
        self._check(value, name=name or self.name)

    def __and__(self, other: Invariant) -> Invariant:
        """Compose two invariants: ``(a & b)(x)`` checks both ``a`` and ``b``."""
        checks = []
        for inv in (self, other):
            if isinstance(inv, _Composed):
                checks.extend(inv._checks)
            else:
                checks.append(inv)
        return _Composed(checks)

    def __repr__(self) -> str:
        return f"Invariant({self.name!r})"


class _Composed(Invariant):
    """Multiple invariants composed with ``&``."""

    def __init__(self, checks: list[Invariant]):
        self._checks = checks
        combined_name = " & ".join(c.name for c in checks)
        super().__init__(combined_name, self._check_all)

    def _check_all(self, value: Any, name: str | None = None) -> None:
        for inv in self._checks:
            inv(value, name=name or inv.name)


# ---------------------------------------------------------------------------
# Built-in invariants
# ---------------------------------------------------------------------------


def _check_no_nan(value: Any, name: str = "no_nan") -> None:
    if isinstance(value, float) and math.isnan(value):
        raise AssertionError(f"Invariant '{name}' violated: found NaN")
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            if isinstance(v, float) and math.isnan(v):
                raise AssertionError(f"Invariant '{name}' violated: NaN at index {i}")
    # numpy support (optional)
    if hasattr(value, "shape"):
        try:
            import numpy as np

            if np.isnan(value).any():
                idx = tuple(np.argwhere(np.isnan(value))[0])
                raise AssertionError(f"Invariant '{name}' violated: NaN at {idx}")
        except (TypeError, ImportError):
            pass


def _check_no_inf(value: Any, name: str = "no_inf") -> None:
    if isinstance(value, float) and math.isinf(value):
        raise AssertionError(f"Invariant '{name}' violated: found Inf")
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            if isinstance(v, float) and math.isinf(v):
                raise AssertionError(f"Invariant '{name}' violated: Inf at index {i}")
    if hasattr(value, "shape"):
        try:
            import numpy as np

            if np.isinf(value).any():
                idx = tuple(np.argwhere(np.isinf(value))[0])
                raise AssertionError(f"Invariant '{name}' violated: Inf at {idx}")
        except (TypeError, ImportError):
            pass


# Rejects NaN in scalars, sequences, and numpy arrays.
no_nan = Invariant("no_nan", _check_no_nan)
# Rejects Inf / -Inf in scalars, sequences, and numpy arrays.
no_inf = Invariant("no_inf", _check_no_inf)
# Shorthand for no_nan & no_inf -- rejects any non-finite float.
finite = no_nan & no_inf


# ---------------------------------------------------------------------------
# Invariant factories (parameterised)
# ---------------------------------------------------------------------------


def bounded(lo: float, hi: float) -> Invariant:
    """Value (or all elements) must be in [lo, hi]."""

    def check(value: Any, name: str = f"bounded({lo}, {hi})") -> None:
        if isinstance(value, (int, float)):
            if not (lo <= value <= hi):
                raise AssertionError(f"Invariant '{name}' violated: {value} not in [{lo}, {hi}]")
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                if isinstance(v, (int, float)) and not (lo <= v <= hi):
                    raise AssertionError(
                        f"Invariant '{name}' violated: {v} at index {i} not in [{lo}, {hi}]"
                    )
        elif hasattr(value, "shape"):
            try:
                import numpy as np  # noqa: F401

                if (value < lo).any() or (value > hi).any():
                    raise AssertionError(
                        f"Invariant '{name}' violated: values outside [{lo}, {hi}]"
                    )
            except (TypeError, ImportError):
                pass

    return Invariant(f"bounded({lo}, {hi})", check)


def monotonic(*, strict: bool = False) -> Invariant:
    """Sequence must be monotonically non-decreasing (or strictly increasing)."""
    label = "strictly_increasing" if strict else "monotonic"

    def check(value: Any, name: str = label) -> None:
        seq = list(value)
        for i in range(1, len(seq)):
            if strict and seq[i] <= seq[i - 1]:
                raise AssertionError(
                    f"Invariant '{name}' violated: {seq[i]} <= {seq[i - 1]} at index {i}"
                )
            elif not strict and seq[i] < seq[i - 1]:
                raise AssertionError(
                    f"Invariant '{name}' violated: {seq[i]} < {seq[i - 1]} at index {i}"
                )

    return Invariant(label, check)


def unique(*, key: Callable | None = None) -> Invariant:
    """All elements must be unique (optionally by *key*)."""

    def check(value: Any, name: str = "unique") -> None:
        seen: set = set()
        for item in value:
            k = key(item) if key else item
            if k in seen:
                raise AssertionError(f"Invariant '{name}' violated: duplicate {item!r}")
            seen.add(k)

    return Invariant("unique", check)


def non_empty() -> Invariant:
    """Value must not be empty / falsy."""

    def check(value: Any, name: str = "non_empty") -> None:
        if not value and value != 0:
            raise AssertionError(f"Invariant '{name}' violated: value is empty")

    return Invariant("non_empty", check)
