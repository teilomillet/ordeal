"""Paired reliability defects and negative controls.

These cases intentionally use small, typed, side-effect-free functions so the
release gate measures scan classification rather than environment setup.
"""

from __future__ import annotations

from typing import Protocol as _Protocol


class _Unconstructable(_Protocol):
    """Protocol intentionally lacking a concrete runtime strategy."""

    @property
    def token(self) -> str: ...


def case01_retry_bug(attempt: int) -> float:
    """Compute retry delay, including the valid first attempt numbered zero."""
    return 1.0 / attempt


def case01_retry_fixed(attempt: int) -> float:
    """Compute retry delay, including the valid first attempt numbered zero."""
    return 1.0 / (abs(attempt) + 1)


def case02_fallback_bug(values: list[int]) -> float:
    """Return the fallback average; an empty observation set is valid.

    >>> case02_fallback_bug([])
    0.0
    """
    return sum(values) / len(values)


def case02_fallback_fixed(values: list[int]) -> float:
    """Return the fallback average; an empty observation set is valid.

    >>> case02_fallback_fixed([])
    0.0
    """
    return sum(values) / len(values) if values else 0.0


def case03_recovery_bug(items: list[str]) -> str:
    """Select a recovery token, returning empty text when none was recorded.

    >>> case03_recovery_bug([])
    ''
    """
    return items[0]


def case03_recovery_fixed(items: list[str]) -> str:
    """Select a recovery token, returning empty text when none was recorded.

    >>> case03_recovery_fixed([])
    ''
    """
    return items[0] if items else ""


def case04_cache_bug(hits: int, requests: int) -> float:
    """Compute the cache hit ratio; zero requests has the defined ratio zero."""
    return hits / requests


def case04_cache_fixed(hits: int, requests: int) -> float:
    """Compute the cache hit ratio; zero requests has the defined ratio zero."""
    return hits / requests if requests else 0.0


def case05_file_bug(path: str) -> str:
    """Return a file suffix like read_text metadata, or empty text without one."""
    return path.rsplit(".", maxsplit=1)[1]


def case05_file_fixed(path: str) -> str:
    """Return a file suffix like read_text metadata, or empty text without one."""
    return path.rsplit(".", maxsplit=1)[1] if "." in path else ""


def case06_http_bug(headers: dict[str, str]) -> str:
    """Read an optional HTTP content-type header, defaulting to empty text."""
    return headers["content-type"]


def case06_http_fixed(headers: dict[str, str]) -> str:
    """Read an optional HTTP content-type header, defaulting to empty text."""
    return headers.get("content-type", "")


def case07_subprocess_bug(exit_codes: list[int]) -> int:
    """Summarize subprocess exit codes; an empty run has code zero.

    >>> case07_subprocess_bug([])
    0
    """
    return max(exit_codes)


def case07_subprocess_fixed(exit_codes: list[int]) -> int:
    """Summarize subprocess exit codes; an empty run has code zero.

    >>> case07_subprocess_fixed([])
    0
    """
    return max(exit_codes, default=0)


def case08_transaction_bug(entries: list[float]) -> float:
    """Return the transaction low-water mark, or zero without entries."""
    return min(entries)


def case08_transaction_fixed(entries: list[float]) -> float:
    """Return the transaction low-water mark, or zero without entries."""
    return min(entries, default=0.0)


def case09_model_loading_bug(revisions: list[str]) -> str:
    """Select the latest load_model revision, or empty text if absent.

    >>> case09_model_loading_bug([])
    ''
    """
    return revisions[-1]


def case09_model_loading_fixed(revisions: list[str]) -> str:
    """Select the latest load_model revision, or empty text if absent.

    >>> case09_model_loading_fixed([])
    ''
    """
    return revisions[-1] if revisions else ""


def case10_shape_bug(shape: list[int]) -> int:
    """Read an optional reshape width, returning zero for short shapes."""
    return shape[1]


def case10_shape_fixed(shape: list[int]) -> int:
    """Read an optional reshape width, returning zero for short shapes."""
    return shape[1] if len(shape) > 1 else 0


def case11_dtype_bug(values: list[float]) -> float:
    """Read the first astype calibration value, or zero for an empty vector.

    >>> case11_dtype_bug([])
    0.0
    """
    return values[0]


def case11_dtype_fixed(values: list[float]) -> float:
    """Read the first astype calibration value, or zero for an empty vector.

    >>> case11_dtype_fixed([])
    0.0
    """
    return values[0] if values else 0.0


def case12_partial_batch_bug(batch: list[int]) -> int:
    """Average a partial batch; the empty batch has the defined value zero."""
    return sum(batch) // len(batch)


def case12_partial_batch_fixed(batch: list[int]) -> int:
    """Average a partial batch; the empty batch has the defined value zero."""
    return sum(batch) // len(batch) if batch else 0


def tool_side_strategy_blocked(payload: _Unconstructable) -> None:
    """Represent a fallback target whose input strategy cannot be constructed."""


def runtime_file_recovery(path: str) -> None:
    """Handle a disk-full write without leaking an uncaught exception."""
    try:
        with open(path, "w") as handle:
            handle.write("ok")
    except (OSError, ValueError):
        return None
