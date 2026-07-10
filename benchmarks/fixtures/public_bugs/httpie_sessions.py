"""Minimal reproduction of BugsInPy httpie bug 3.

Upstream fix: https://github.com/httpie/cli/commit/589887939507ff26d36ec74bd2c045819cfa3d56
"""

from __future__ import annotations

from types import NoneType
from typing import Literal


def update_headers(request_headers: dict[Literal["X-Ordeal"], NoneType]) -> dict[str, str]:
    """Return the session headers after applying request headers.

    This preserves the upstream bug on its exact regression boundary: an
    explicitly unset header has value ``None`` and is decoded unconditionally.
    """
    stored: dict[str, str] = {}
    for name, value in request_headers.items():
        stored[name] = value.decode("utf-8")
    return stored
