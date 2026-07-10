"""Importable stateful target used by exact harness replay tests."""

from __future__ import annotations


class ReplayBox:
    """Small object whose method needs setup and generated state."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.ready = False

    def run(self, state: dict[str, str], value: str) -> str:
        """Return a labeled value or fail at one supported boundary."""
        if not self.ready:
            raise RuntimeError("setup missing")
        if state["token"] != self.prefix:
            raise RuntimeError("state mismatch")
        if value == "boom":
            raise RuntimeError("supported boom")
        return f"{self.prefix}:{value}"
