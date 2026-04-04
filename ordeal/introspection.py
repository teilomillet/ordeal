"""Shared introspection helpers for resilient annotation handling."""

from __future__ import annotations

import inspect
from typing import get_type_hints


def safe_get_annotations(obj: object) -> dict[str, object]:
    """Return annotations without raising on unresolved names.

    Tries resolved type hints first, then falls back to raw annotations
    so lazy exports and forward references remain visible instead of
    crashing the caller.
    """
    try:
        annotations = get_type_hints(obj, include_extras=True)
    except Exception:
        annotations = None
    if annotations:
        return dict(annotations)

    try:
        raw = inspect.get_annotations(obj, eval_str=False)
    except Exception:
        raw = None
    if raw:
        return dict(raw)

    fallback = getattr(obj, "__annotations__", None)
    if isinstance(fallback, dict):
        return dict(fallback)
    return {}


def annotation_is_none(annotation: object) -> bool:
    """Return True when *annotation* denotes ``None``."""
    if annotation is None or annotation is type(None):
        return True
    if isinstance(annotation, str):
        return annotation in {"None", "NoneType"}
    return False
