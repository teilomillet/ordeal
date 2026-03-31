"""Deprecated — use :mod:`ordeal.integrations.openapi` instead.

This module exists only for backward compatibility.  All functionality
has moved to the built-in OpenAPI engine.  Importing from here still
works but emits a :class:`DeprecationWarning`.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "ordeal.integrations.schemathesis_ext is deprecated. "
    "Import from ordeal.integrations.openapi instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export public API so existing imports keep working.
from ordeal.integrations.openapi import (  # noqa: F401, E402
    ChaosAPIResult,
    _discover_handlers,
    _FaultScheduler,
    _TraceCollector,
    auto_faults,
    chaos_api_test,
    with_chaos,
)

__all__ = [
    "ChaosAPIResult",
    "ChaosAPIHook",
    "auto_faults",
    "with_chaos",
    "chaos_api_test",
]


class ChaosAPIHook:
    """Removed — schemathesis is no longer a dependency.

    Use :func:`ordeal.integrations.openapi.chaos_api_test` instead, which
    handles fault scheduling internally.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "ChaosAPIHook has been removed (schemathesis is no longer a dependency). "
            "Use chaos_api_test() or with_chaos() from ordeal.integrations.openapi instead."
        )
