"""Fixed sibling of the BugsInPy HTTPie bug 3 reproduction.

Upstream fix: https://github.com/httpie/cli/commit/589887939507ff26d36ec74bd2c045819cfa3d56
"""

from __future__ import annotations

from types import NoneType
from typing import Literal


def update_headers(request_headers: dict[Literal["X-Ordeal"], NoneType]) -> dict[str, str]:
    """Return session headers while skipping explicitly unset values."""
    stored: dict[str, str] = {}
    for name, value in request_headers.items():
        if value is None:
            continue
        stored[name] = value.decode("utf-8")
    return stored
