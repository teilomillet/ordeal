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


def unit_normalized(*, tol: float = 1e-6) -> Invariant:
    """Each row vector has L2 norm within *tol* of 1.0.

    For 1-D arrays, checks the single vector norm.
    For 2-D arrays, checks each row independently.
    """

    def check(value: Any, name: str = "unit_normalized") -> None:
        arr = _to_numpy(value)
        if arr is None:
            raise AssertionError(f"Invariant '{name}': cannot convert to array")
        import numpy as np

        if arr.ndim == 1:
            norm = float(np.linalg.norm(arr))
            if abs(norm - 1.0) > tol:
                raise AssertionError(
                    f"Invariant '{name}' violated: norm={norm:.8f}, expected ~1.0"
                )
        elif arr.ndim == 2:
            norms = np.linalg.norm(arr, axis=1)
            for i, n in enumerate(norms):
                if abs(n - 1.0) > tol:
                    raise AssertionError(
                        f"Invariant '{name}' violated: row {i} norm={n:.8f}, expected ~1.0"
                    )
        else:
            raise AssertionError(
                f"Invariant '{name}': expected 1-D or 2-D array, got {arr.ndim}-D"
            )

    return Invariant("unit_normalized", check)


def orthonormal(*, tol: float = 1e-6) -> Invariant:
    """Rows of a 2-D array form an orthonormal set.

    Checks that ``M @ M.T`` is close to the identity matrix.
    """

    def check(value: Any, name: str = "orthonormal") -> None:
        arr = _to_numpy(value)
        if arr is None:
            raise AssertionError(f"Invariant '{name}': cannot convert to array")
        import numpy as np

        if arr.ndim != 2:
            raise AssertionError(f"Invariant '{name}': expected 2-D array, got {arr.ndim}-D")
        gram = arr @ arr.T
        identity = np.eye(gram.shape[0])
        max_err = float(np.max(np.abs(gram - identity)))
        if max_err > tol:
            raise AssertionError(
                f"Invariant '{name}' violated: max |G - I| = {max_err:.8f} > {tol}"
            )

    return Invariant("orthonormal", check)


def symmetric(*, tol: float = 1e-6) -> Invariant:
    """2-D array is symmetric: ``M == M.T`` within tolerance."""

    def check(value: Any, name: str = "symmetric") -> None:
        arr = _to_numpy(value)
        if arr is None:
            raise AssertionError(f"Invariant '{name}': cannot convert to array")
        import numpy as np

        if arr.ndim != 2 or arr.shape[0] != arr.shape[1]:
            raise AssertionError(
                f"Invariant '{name}': expected square 2-D array, got shape {arr.shape}"
            )
        max_err = float(np.max(np.abs(arr - arr.T)))
        if max_err > tol:
            raise AssertionError(
                f"Invariant '{name}' violated: max |M - M.T| = {max_err:.8f} > {tol}"
            )

    return Invariant("symmetric", check)


def positive_semi_definite(*, tol: float = 1e-6) -> Invariant:
    """All eigenvalues of a square matrix are >= -tol."""

    def check(value: Any, name: str = "positive_semi_definite") -> None:
        arr = _to_numpy(value)
        if arr is None:
            raise AssertionError(f"Invariant '{name}': cannot convert to array")
        import numpy as np

        if arr.ndim != 2 or arr.shape[0] != arr.shape[1]:
            raise AssertionError(
                f"Invariant '{name}': expected square 2-D array, got shape {arr.shape}"
            )
        eigenvalues = np.linalg.eigvalsh(arr)
        min_eig = float(np.min(eigenvalues))
        if min_eig < -tol:
            raise AssertionError(
                f"Invariant '{name}' violated: min eigenvalue = {min_eig:.8f}"
            )

    return Invariant("positive_semi_definite", check)


def rank_bounded(min_rank: int = 0, max_rank: int | None = None) -> Invariant:
    """Matrix rank is within [min_rank, max_rank]."""

    label = f"rank_bounded({min_rank}, {max_rank})"

    def check(value: Any, name: str = label) -> None:
        arr = _to_numpy(value)
        if arr is None:
            raise AssertionError(f"Invariant '{name}': cannot convert to array")
        import numpy as np

        r = int(np.linalg.matrix_rank(arr))
        if r < min_rank:
            raise AssertionError(
                f"Invariant '{name}' violated: rank={r} < min_rank={min_rank}"
            )
        if max_rank is not None and r > max_rank:
            raise AssertionError(
                f"Invariant '{name}' violated: rank={r} > max_rank={max_rank}"
            )

    return Invariant(label, check)


# ---------------------------------------------------------------------------
# Statistical invariants
# ---------------------------------------------------------------------------


def mean_bounded(lo: float, hi: float) -> Invariant:
    """Mean of a numeric sequence must be in [lo, hi]."""
    label = f"mean_bounded({lo}, {hi})"

    def check(value: Any, name: str = label) -> None:
        arr = _to_numpy(value)
        if arr is not None:
            import numpy as np

            m = float(np.mean(arr))
        else:
            seq = list(value)
            if not seq:
                raise AssertionError(f"Invariant '{name}': empty sequence")
            m = sum(seq) / len(seq)
        if not (lo <= m <= hi):
            raise AssertionError(
                f"Invariant '{name}' violated: mean={m:.6f} not in [{lo}, {hi}]"
            )

    return Invariant(label, check)


def variance_bounded(lo: float, hi: float) -> Invariant:
    """Variance of a numeric sequence must be in [lo, hi]."""
    label = f"variance_bounded({lo}, {hi})"

    def check(value: Any, name: str = label) -> None:
        arr = _to_numpy(value)
        if arr is not None:
            import numpy as np

            v = float(np.var(arr))
        else:
            seq = list(value)
            if not seq:
                raise AssertionError(f"Invariant '{name}': empty sequence")
            m = sum(seq) / len(seq)
            v = sum((x - m) ** 2 for x in seq) / len(seq)
        if not (lo <= v <= hi):
            raise AssertionError(
                f"Invariant '{name}' violated: variance={v:.6f} not in [{lo}, {hi}]"
            )

    return Invariant(label, check)
