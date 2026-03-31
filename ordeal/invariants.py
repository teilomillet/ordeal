"""Named, composable invariant assertions.

Invariants are reusable checks with clear failure messages.
They compose with the ``&`` operator to build complex checks from
simple building blocks::

    from ordeal.invariants import no_nan, no_inf, bounded, finite

    # Compose invariants with &
    valid_score = bounded(0, 1) & finite      # bounded + no NaN + no Inf
    valid_score(model_output)                  # raises AssertionError on violation
    valid_score(model_output, name="score")    # custom name in message

    # The & operator is associative — chain as many as you like
    strict = bounded(0, 1) & finite & monotonic(strict=True)

    # Built-in `finite` is itself a composition: no_nan & no_inf
    finite(model_output)  # rejects NaN and Inf in one call

Array-like values from MLX, JAX, and PyTorch are auto-converted to
numpy arrays before checking. Numpy is optional — invariants that
only need scalars or sequences work without it.

Discover all available invariants programmatically::

    from ordeal.invariants import catalog
    for entry in catalog():
        print(f"{entry['name']}  -- {entry['doc']}")
"""

from __future__ import annotations

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
                deviation = norm - 1.0
                raise AssertionError(
                    f"Invariant '{name}' violated: norm = {norm:.8f} "
                    f"(expected 1.0, deviation: {deviation:+.8f}, tol: {tol})"
                )
        elif arr.ndim == 2:
            norms = np.linalg.norm(arr, axis=1)
            deviations = np.abs(norms - 1.0)
            worst_row = int(np.argmax(deviations))
            worst_norm = float(norms[worst_row])
            worst_dev = float(deviations[worst_row])
            if worst_dev > tol:
                deviation = worst_norm - 1.0
                raise AssertionError(
                    f"Invariant '{name}' violated: row {worst_row} norm = {worst_norm:.8f} "
                    f"(expected 1.0, deviation: {deviation:+.8f}, tol: {tol})"
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
        diff = np.abs(gram - identity)
        max_err = float(np.max(diff))
        if max_err > tol:
            worst_flat = int(np.argmax(diff))
            worst_row, worst_col = np.unravel_index(worst_flat, diff.shape)
            actual_val = float(gram[worst_row, worst_col])
            expected_val = float(identity[worst_row, worst_col])
            raise AssertionError(
                f"Invariant '{name}' violated: "
                f"(M @ M.T)[{int(worst_row)}, {int(worst_col)}] = {actual_val:.8f} "
                f"(expected {expected_val:.8f}, deviation: {max_err:.8f}, tol: {tol})"
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
        diff = np.abs(arr - arr.T)
        max_err = float(np.max(diff))
        if max_err > tol:
            worst_flat = int(np.argmax(diff))
            worst_row, worst_col = np.unravel_index(worst_flat, diff.shape)
            actual_val = float(arr[worst_row, worst_col])
            transpose_val = float(arr[worst_col, worst_row])
            raise AssertionError(
                f"Invariant '{name}' violated: "
                f"M[{int(worst_row)}, {int(worst_col)}] = {actual_val:.8f} "
                f"but M[{int(worst_col)}, {int(worst_row)}] = {transpose_val:.8f} "
                f"(deviation: {max_err:.8f}, tol: {tol})"
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
            min_idx = int(np.argmin(eigenvalues))
            raise AssertionError(
                f"Invariant '{name}' violated: eigenvalue[{min_idx}] = {min_eig:.8f} "
                f"(expected >= {-tol:.8f}, deviation: {abs(min_eig):.8f})"
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
            raise AssertionError(f"Invariant '{name}' violated: rank={r} < min_rank={min_rank}")
        if max_rank is not None and r > max_rank:
            raise AssertionError(f"Invariant '{name}' violated: rank={r} > max_rank={max_rank}")

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
            raise AssertionError(f"Invariant '{name}' violated: mean={m:.6f} not in [{lo}, {hi}]")

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


# ---------------------------------------------------------------------------
# Catalog — introspect available invariants at runtime
# ---------------------------------------------------------------------------


def catalog() -> list[dict[str, str]]:
    """Discover all available invariants via runtime introspection.

    Returns a list of dicts with ``name``, ``type`` (instance or factory),
    ``doc``, and ``signature`` (for factories).  Derived from module globals
    — new invariants appear automatically.
    """
    import inspect as _inspect
    import sys

    mod = sys.modules[__name__]
    entries: list[dict[str, str]] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_"):
            continue
        obj = getattr(mod, attr_name)
        if isinstance(obj, Invariant):
            entries.append(
                {
                    "name": attr_name,
                    "type": "instance",
                    "doc": getattr(obj, "__doc__", None) or repr(obj),
                    "signature": "(value, *, name=None)",
                }
            )
        elif callable(obj) and not _inspect.isclass(obj) and attr_name != "catalog":
            try:
                sig = _inspect.signature(obj)
                ret = sig.return_annotation
                if ret is _inspect.Parameter.empty:
                    continue
                ret_name = getattr(ret, "__name__", str(ret))
                if "Invariant" not in ret_name:
                    continue
            except (ValueError, TypeError):
                continue
            entries.append(
                {
                    "name": attr_name,
                    "type": "factory",
                    "doc": (_inspect.getdoc(obj) or "").split("\n")[0],
                    "signature": str(sig),
                }
            )
    return entries
