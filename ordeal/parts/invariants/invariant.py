from __future__ import annotations
# ruff: noqa
import math
from typing import Any, Callable
_MISSING = object()
class Invariant:
    """A named, composable assertion."""

    def __init__(self, name: str, check_fn: Callable[..., None]):
        self.name = name
        self._check = check_fn

    def __call__(self, value: Any = _MISSING, *, name: str | None = None) -> Any:
        """Run the invariant check, or return self if called with no args.

        This makes both patterns work::

            finite(value)   # check immediately
            finite()        # returns the invariant (for use as a parameter)
        """
        if value is _MISSING:
            return self
        self._check(value, name=name or self.name)
        return None

    def __and__(self, other: Invariant) -> Invariant:
        """Compose two invariants: ``(a & b)(x)`` checks both ``a`` and ``b``.

        Use this to build domain-specific compound checks from simple
        invariants::

            check = bounded(0, 1) & finite
            check(value)  # raises on out-of-range, NaN, or Inf

        The operator is associative and flattens nested compositions,
        so ``a & b & c`` produces a single flat check list.
        """
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
    # numpy support (optional) — auto-converts MLX, JAX, PyTorch arrays
    if hasattr(value, "shape"):
        try:
            import numpy as np

            if not isinstance(value, np.ndarray):
                value = np.asarray(value)
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
    # Auto-converts MLX, JAX, PyTorch arrays
    if hasattr(value, "shape"):
        try:
            import numpy as np

            if not isinstance(value, np.ndarray):
                value = np.asarray(value)
            if np.isinf(value).any():
                idx = tuple(np.argwhere(np.isinf(value))[0])
                raise AssertionError(f"Invariant '{name}' violated: Inf at {idx}")
        except (TypeError, ImportError):
            pass
no_nan = Invariant("no_nan", _check_no_nan)
no_nan.__doc__ = "Rejects NaN in scalars, sequences, and numpy arrays."
no_inf = Invariant("no_inf", _check_no_inf)
no_inf.__doc__ = "Rejects Inf / -Inf in scalars, sequences, and numpy arrays."
finite = no_nan & no_inf
finite.__doc__ = "Rejects any non-finite float (NaN or Inf). Shorthand for no_nan & no_inf."
# ---------------------------------------------------------------------------
# Invariant factories (parameterised)
# ---------------------------------------------------------------------------


def bounded(lo: float, hi: float) -> Invariant:
    """Value (or all elements) must be in [lo, hi]."""

    def check(value: Any, name: str = f"bounded({lo}, {hi})") -> None:
        if isinstance(value, (int, float)):
            if not (lo <= value <= hi):
                if value < lo:
                    deviation = lo - value
                    raise AssertionError(
                        f"Invariant '{name}' violated: value = {value} "
                        f"(expected >= {lo}, deviation: -{deviation})"
                    )
                else:
                    deviation = value - hi
                    raise AssertionError(
                        f"Invariant '{name}' violated: value = {value} "
                        f"(expected <= {hi}, deviation: +{deviation})"
                    )
        elif isinstance(value, (list, tuple)):
            worst_idx, worst_val, worst_dev = None, None, 0.0
            for i, v in enumerate(value):
                if isinstance(v, (int, float)):
                    if v < lo and (lo - v) > worst_dev:
                        worst_idx, worst_val, worst_dev = i, v, lo - v
                    elif v > hi and (v - hi) > worst_dev:
                        worst_idx, worst_val, worst_dev = i, v, v - hi
            if worst_idx is not None:
                if worst_val < lo:
                    raise AssertionError(
                        f"Invariant '{name}' violated: value[{worst_idx}] = {worst_val} "
                        f"(expected >= {lo}, deviation: -{worst_dev})"
                    )
                else:
                    raise AssertionError(
                        f"Invariant '{name}' violated: value[{worst_idx}] = {worst_val} "
                        f"(expected <= {hi}, deviation: +{worst_dev})"
                    )
        elif hasattr(value, "shape"):
            try:
                import numpy as np

                if not isinstance(value, np.ndarray):
                    value = np.asarray(value)
                below = value < lo
                above = value > hi
                if below.any() or above.any():
                    # Find the worst violation
                    worst_dev = 0.0
                    worst_idx = None
                    worst_val = None
                    if below.any():
                        dev_below = lo - value[below]
                        max_below = float(np.max(dev_below))
                        if max_below > worst_dev:
                            worst_dev = max_below
                            idx_flat = int(np.argmax(lo - value * below.astype(float)))
                            worst_idx = np.unravel_index(idx_flat, value.shape)
                            worst_val = float(value.flat[idx_flat])
                    if above.any():
                        dev_above = value[above] - hi
                        max_above = float(np.max(dev_above))
                        if max_above > worst_dev:
                            worst_dev = max_above
                            idx_flat = int(np.argmax(value * above.astype(float) - hi))
                            worst_idx = np.unravel_index(idx_flat, value.shape)
                            worst_val = float(value.flat[idx_flat])
                    if worst_idx is not None:
                        idx_str = (
                            str(worst_idx[0])
                            if len(worst_idx) == 1
                            else str(tuple(int(x) for x in worst_idx))
                        )
                        if worst_val < lo:
                            raise AssertionError(
                                f"Invariant '{name}' violated: value[{idx_str}] = {worst_val} "
                                f"(expected >= {lo}, deviation: -{worst_dev})"
                            )
                        else:
                            raise AssertionError(
                                f"Invariant '{name}' violated: value[{idx_str}] = {worst_val} "
                                f"(expected <= {hi}, deviation: +{worst_dev})"
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
# ---------------------------------------------------------------------------
# Tensor / array invariants (numpy-guarded)
# ---------------------------------------------------------------------------


def _to_numpy(value: Any) -> Any:
    """Convert array-like to numpy, if possible."""
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            return value
        if hasattr(value, "__array__"):
            return value.__array__()
        return np.asarray(value)
    except (ImportError, TypeError, ValueError):
        return None
