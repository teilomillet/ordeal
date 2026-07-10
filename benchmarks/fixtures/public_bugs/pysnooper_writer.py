"""Minimal reproduction of BugsInPy PySnooper bug 3.

Upstream fix: https://github.com/cool-RR/PySnooper/commit/15555ed760000b049aff8fecc79d29339c1224c3
"""

from __future__ import annotations

from typing import Literal


class _MemoryOutput:
    """Small file-like context manager that avoids real filesystem writes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.text = ""

    def __enter__(self) -> _MemoryOutput:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def write(self, text: str) -> None:
        """Append text like a file opened in append mode."""
        self.text += text


def _memory_open(path: str, mode: Literal["a"]) -> _MemoryOutput:
    """Return an isolated append-only output sink."""
    assert mode == "a"
    return _MemoryOutput(path)


def write_to_path(output: Literal["trace.log"]) -> dict[str, str]:
    """Create and invoke the path-output writer closure.

    The upstream buggy closure referenced ``output_path`` even though its
    enclosing function argument was named ``output``.
    """

    def write(text: str) -> dict[str, str]:
        with _memory_open(output_path, "a") as output_file:  # type: ignore[name-defined]  # noqa: F821
            output_file.write(text)
            return {"path": output_file.path, "text": output_file.text}

    return write("trace")
