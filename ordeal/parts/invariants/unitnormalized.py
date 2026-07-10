from __future__ import annotations
# ruff: noqa
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
