from __future__ import annotations
# ruff: noqa
def _apply_pydantic_constraints(
    base: st.SearchStrategy,
    field_info: Any,
    annotation: type,
) -> st.SearchStrategy:
    """Narrow a strategy based on Pydantic field metadata constraints.

    Pydantic v2 stores constraints as annotated_types objects in
    ``field_info.metadata``, e.g. ``[Ge(ge=0), Le(le=100)]``.
    """
    metadata = getattr(field_info, "metadata", [])
    if not metadata:
        return base

    # Extract annotated_types constraints (Pydantic v2 uses these)
    ge = gt = le = lt = None
    min_length = max_length = None

    for m in metadata:
        val = getattr(m, "ge", None)
        if val is not None:
            ge = val
        val = getattr(m, "gt", None)
        if val is not None:
            gt = val
        val = getattr(m, "le", None)
        if val is not None:
            le = val
        val = getattr(m, "lt", None)
        if val is not None:
            lt = val
        val = getattr(m, "min_length", None)
        if val is not None:
            min_length = val
        val = getattr(m, "max_length", None)
        if val is not None:
            max_length = val

    # Apply numeric constraints
    if annotation is int or annotation is float:
        min_val = None
        max_val = None
        if ge is not None:
            min_val = ge
        elif gt is not None:
            min_val = gt + (1 if annotation is int else 1e-10)
        if le is not None:
            max_val = le
        elif lt is not None:
            max_val = lt - (1 if annotation is int else 1e-10)

        if min_val is not None or max_val is not None:
            if annotation is int:
                return biased.integers(
                    min_value=int(min_val) if min_val is not None else None,
                    max_value=int(max_val) if max_val is not None else None,
                )
            else:
                return biased.floats(
                    min_value=float(min_val) if min_val is not None else None,
                    max_value=float(max_val) if max_val is not None else None,
                )

    # Apply string length constraints
    if annotation is str and (min_length is not None or max_length is not None):
        return biased.strings(
            min_size=min_length or 0,
            max_size=max_length or 100,
        )

    return base
# ============================================================================
# Convenience: from_type alias
# ============================================================================

from_type = strategy_for_type
