"""Demo module for trying ordeal without any setup.

Run these immediately with uvx:

    uvx ordeal mine ordeal.demo.score
    uvx ordeal mine-pair ordeal.demo.encode ordeal.demo.decode
    uvx ordeal audit ordeal.demo

Every function here is type-annotated so ordeal can generate inputs
and discover properties automatically.
"""

from __future__ import annotations

import math


def score(x: float) -> float:
    """Compute a bounded score. Output is always in [0, 1]."""
    return max(0.0, min(1.0, x * 0.5 + 0.5))


def clamp(value: int, lo: int, hi: int) -> int:
    """Clamp value to [lo, hi]. Has a subtle bug when lo > hi."""
    return max(lo, min(hi, value))


def encode(s: str) -> str:
    """Simple reversible encoding."""
    return s[::-1]


def decode(s: str) -> str:
    """Inverse of encode."""
    return s[::-1]


def normalize(xs: list[float]) -> list[float]:
    """Normalize a list to sum to 1. Returns empty list if input is empty."""
    if not xs:
        return []
    total = sum(xs)
    if total == 0:
        return [1.0 / len(xs)] * len(xs)
    return [x / total for x in xs]


def safe_div(a: float, b: float) -> float:
    """Division that returns 0.0 on divide-by-zero instead of raising."""
    if b == 0:
        return 0.0
    return a / b


def distance(x: float, y: float) -> float:
    """Euclidean distance from origin. Always non-negative."""
    return math.sqrt(x * x + y * y)
