"""Numerical fault injections — numeric and ML/data seam corruptors.

- nan_injection(target) — inject NaN into numeric output
- inf_injection(target) — inject Inf into numeric output
- wrong_shape(target, expected, actual) — return array with wrong shape
- dtype_drift(target, kind) — coerce numeric outputs to another dtype
- partial_batch(target, fraction, min_items) — truncate batch-like outputs
- feature_order_drift(target, shift) — rotate feature order without changing shape
- missing_feature(target, key, fill) — drop or blank one feature in dict-like outputs
- corrupted_floats(corrupt_type) — provide corrupt floats via fault.value()

::

    from ordeal.faults.numerical import dtype_drift, nan_injection, wrong_shape
    faults = [nan_injection("myapp.model.predict"),
              dtype_drift("myapp.model.predict", kind="str"),
              wrong_shape("myapp.model.predict", expected=(1, 512), actual=(1, 256))]
"""

from __future__ import annotations

import functools
from typing import Any, Literal

from . import Fault, PatchFault

#: Valid values for the ``corrupt_type`` parameter of :func:`corrupted_floats`.
CorruptType = Literal["nan", "inf", "-inf", "max", "min"]
DTypeDriftKind = Literal["str", "int", "bool", "object"]
_DROP_FEATURE = object()


def _transform_numeric_leaves(value: Any, transform: Any) -> Any:
    """Apply *transform* to every numeric leaf in *value*."""
    if isinstance(value, (int, float)):
        return transform(value)
    if isinstance(value, list):
        return [_transform_numeric_leaves(item, transform) for item in value]
    if isinstance(value, tuple):
        return tuple(_transform_numeric_leaves(item, transform) for item in value)
    if isinstance(value, dict):
        return {key: _transform_numeric_leaves(item, transform) for key, item in value.items()}
    if hasattr(value, "tolist"):
        try:
            return _transform_numeric_leaves(value.tolist(), transform)
        except Exception:
            return value
    return value


def _corrupt_numeric(value: Any, corrupt: float) -> Any:
    """Replace numeric content in *value* with *corrupt*."""
    return _transform_numeric_leaves(value, lambda _leaf: corrupt)


def _truncate_batch(value: Any, fraction: float, min_items: int) -> Any:
    """Keep only the leading fraction of a batch-like output."""
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0.0 and 1.0")
    if min_items < 0:
        raise ValueError("min_items must be >= 0")
    if isinstance(value, list):
        keep = min(len(value), max(min_items, int(len(value) * fraction))) if value else 0
        return value[:keep]
    if isinstance(value, tuple):
        keep = min(len(value), max(min_items, int(len(value) * fraction))) if value else 0
        return value[:keep]
    if hasattr(value, "shape") and getattr(value, "shape", ()):
        try:
            length = int(value.shape[0])
            keep = min(length, max(min_items, int(length * fraction))) if length else 0
            return value[:keep]
        except Exception:
            return value
    if hasattr(value, "tolist"):
        try:
            return _truncate_batch(value.tolist(), fraction, min_items)
        except Exception:
            return value
    return value


def _rotate_values(values: list[Any], shift: int) -> list[Any]:
    """Return *values* rotated left by *shift* positions."""
    if len(values) <= 1:
        return list(values)
    offset = shift % len(values)
    return values[offset:] + values[:offset]


def _shift_features(value: Any, shift: int) -> Any:
    """Rotate feature order while preserving the outer batch shape."""
    if isinstance(value, dict):
        ordered_keys = _rotate_values(list(value.keys()), shift)
        return {key: value[key] for key in ordered_keys}
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            return [_shift_features(item, shift) for item in value]
        if value and all(isinstance(item, (list, tuple)) for item in value):
            return [_shift_features(item, shift) for item in value]
        return _rotate_values(list(value), shift)
    if isinstance(value, tuple):
        if value and all(isinstance(item, dict) for item in value):
            return tuple(_shift_features(item, shift) for item in value)
        if value and all(isinstance(item, (list, tuple)) for item in value):
            return tuple(_shift_features(item, shift) for item in value)
        return tuple(_rotate_values(list(value), shift))
    if hasattr(value, "tolist"):
        try:
            return _shift_features(value.tolist(), shift)
        except Exception:
            return value
    return value


def _drop_or_fill_mapping(
    mapping: dict[str, Any],
    key: str | None,
    fill: object,
) -> dict[str, Any]:
    """Drop one mapping entry or replace it with *fill*."""
    if not mapping:
        return mapping
    target_key = key if key in mapping else next(iter(mapping))
    if fill is _DROP_FEATURE:
        return {name: value for name, value in mapping.items() if name != target_key}
    result = dict(mapping)
    result[target_key] = fill
    return result


def _drop_or_fill_feature(value: Any, key: str | None, fill: object) -> Any:
    """Drop or blank one feature in dict-shaped payloads."""
    if isinstance(value, dict):
        return _drop_or_fill_mapping(dict(value), key, fill)
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return [_drop_or_fill_mapping(dict(item), key, fill) for item in value]
    if isinstance(value, tuple) and value and all(isinstance(item, dict) for item in value):
        return tuple(_drop_or_fill_mapping(dict(item), key, fill) for item in value)
    if hasattr(value, "tolist"):
        try:
            return _drop_or_fill_feature(value.tolist(), key, fill)
        except Exception:
            return value
    return value


def nan_injection(target: str) -> PatchFault:
    """Inject NaN into numeric output of *target* — catches missing NaN checks in math/ML."""

    def wrapper(original):
        @functools.wraps(original)
        def injected(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _corrupt_numeric(result, float("nan"))

        return injected

    return PatchFault(target, wrapper, name=f"nan_injection({target})")


def inf_injection(target: str) -> PatchFault:
    """Inject Inf into numeric output of *target* — catches missing overflow checks in math/ML."""

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


def dtype_drift(target: str, kind: DTypeDriftKind = "str") -> PatchFault:
    """Coerce numeric output of *target* into another dtype-like representation."""

    def _coerce(value: float) -> Any:
        match kind:
            case "str":
                return str(value)
            case "int":
                return int(value)
            case "bool":
                return bool(value)
            case "object":
                return {"value": value}
        return str(value)

    def wrapper(original):
        @functools.wraps(original)
        def drifted(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _transform_numeric_leaves(result, _coerce)

        return drifted

    return PatchFault(target, wrapper, name=f"dtype_drift({target}, {kind})")


def partial_batch(target: str, fraction: float = 0.5, min_items: int = 1) -> PatchFault:
    """Return only part of a batch-like output from *target*."""

    def wrapper(original):
        @functools.wraps(original)
        def truncated(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _truncate_batch(result, fraction, min_items)

        return truncated

    return PatchFault(
        target,
        wrapper,
        name=f"partial_batch({target}, fraction={fraction}, min_items={min_items})",
    )


def feature_order_drift(target: str, *, shift: int = 1) -> PatchFault:
    """Rotate feature order in the output of *target* without dropping values."""

    def wrapper(original):
        @functools.wraps(original)
        def reordered(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _shift_features(result, shift)

        return reordered

    return PatchFault(target, wrapper, name=f"feature_order_drift({target}, shift={shift})")


def missing_feature(
    target: str,
    key: str | None = None,
    *,
    fill: object = _DROP_FEATURE,
) -> PatchFault:
    """Drop one feature key from dict-like output, or replace it with *fill*."""

    def wrapper(original):
        @functools.wraps(original)
        def dropped(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            return _drop_or_fill_feature(result, key, fill)

        return dropped

    name = f"missing_feature({target}, key={key!r})"
    if fill is not _DROP_FEATURE:
        name = f"missing_feature({target}, key={key!r}, fill={fill!r})"
    return PatchFault(target, wrapper, name=name)


class _CorruptedFloatsFault(Fault):
    """Standalone fault: when queried, provides corrupted float values.

    Use in rules via ``fault.value()`` rather than patching a target.
    """

    def __init__(self, corrupt_type: CorruptType = "nan") -> None:
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


def corrupted_floats(corrupt_type: CorruptType = "nan") -> _CorruptedFloatsFault:
    """Provide corrupt floats (NaN/Inf/zero/subnormal) via ``fault.value()`` when active.

    Unlike the ``*_injection`` faults, this doesn't patch a function.
    Instead, use ``fault.value()`` in your rules to get corrupt data.

    Args:
        corrupt_type: Kind of corruption — ``"nan"``, ``"inf"``, ``"-inf"``,
            ``"max"`` (DBL_MAX ≈ 1.8e308), or ``"min"`` (smallest positive
            subnormal ≈ 5e-324).
    """
    return _CorruptedFloatsFault(corrupt_type)
