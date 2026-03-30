"""Numerical fault injections.

Target a function and corrupt its numeric output while the fault is active.

    from ordeal.faults.numerical import nan_injection, inf_injection, wrong_shape
    faults = [
        nan_injection("model.predict"),
        wrong_shape("model.predict", expected=(1, 512), actual=(1, 256)),
    ]
"""

from __future__ import annotations

import functools
from typing import Any

from . import Fault, PatchFault


def _corrupt_numeric(value: Any, corrupt: float) -> Any:
    """Replace numeric content in *value* with *corrupt*."""
    if isinstance(value, (int, float)):
        return corrupt
    if isinstance(value, (list, tuple)):
        return type(value)(corrupt if isinstance(v, (int, float)) else v for v in value)
    if isinstance(value, dict):
        return {k: corrupt if isinstance(v, (int, float)) else v for k, v in value.items()}
    # numpy-like (duck-typed)
    if hasattr(value, "copy") and hasattr(value, "flat") and hasattr(value, "shape"):
        result = value.copy()
        try:
            result.flat[0] = corrupt
        except (IndexError, ValueError):
            pass
        return result
    return value


def nan_injection(target: str) -> PatchFault:
    """Inject NaN into the numeric output of *target*."""

    def wrapper(original):
        @functools.wraps(original)
        def injected(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _corrupt_numeric(result, float("nan"))

        return injected

    return PatchFault(target, wrapper, name=f"nan_injection({target})")


def inf_injection(target: str) -> PatchFault:
    """Inject Inf into the numeric output of *target*."""

    def wrapper(original):
        @functools.wraps(original)
        def injected(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _corrupt_numeric(result, float("inf"))

        return injected

    return PatchFault(target, wrapper, name=f"inf_injection({target})")


def wrong_shape(
    target: str,
    expected: tuple[int, ...],
    actual: tuple[int, ...],
) -> PatchFault:
    """Make *target* return an array-like with *actual* shape instead of *expected*.

    Works with numpy arrays (if available) or nested lists.
    """

    def _make_nested(shape: tuple[int, ...]) -> list:
        if len(shape) == 1:
            return [0.0] * shape[0]
        return [_make_nested(shape[1:]) for _ in range(shape[0])]

    def wrapper(original):
        @functools.wraps(original)
        def reshaped(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)  # call for side effects
            try:
                import numpy as np

                return np.zeros(actual)
            except ImportError:
                return _make_nested(actual)

        return reshaped

    return PatchFault(target, wrapper, name=f"wrong_shape({target}, {expected}->{actual})")


class _CorruptedFloatsFault(Fault):
    """Standalone fault: when queried, provides corrupted float values.

    Use in rules via ``fault.value()`` rather than patching a target.
    """

    def __init__(self, corrupt_type: str = "nan") -> None:
        super().__init__(name=f"corrupted_floats({corrupt_type})")
        self._corrupt_type = corrupt_type

    def value(self) -> float:
        """Return a corrupt float value (NaN, Inf, etc.)."""
        if not self.active:
            return 0.0
        match self._corrupt_type:
            case "nan":
                return float("nan")
            case "inf":
                return float("inf")
            case "-inf":
                return float("-inf")
            case "max":
                return 1.7976931348623157e308
            case "min":
                return 5e-324
        return float("nan")

    def _do_activate(self) -> None:
        pass  # no patching, just changes value() output

    def _do_deactivate(self) -> None:
        pass


def corrupted_floats(corrupt_type: str = "nan") -> _CorruptedFloatsFault:
    """Fault that provides corrupt float values when active.

    Unlike the ``*_injection`` faults, this doesn't patch a function.
    Instead, use ``fault.value()`` in your rules to get corrupt data.
    """
    return _CorruptedFloatsFault(corrupt_type)
