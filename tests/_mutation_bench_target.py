"""Dedicated mutation benchmark targets with ordinary direct tests."""

from __future__ import annotations


def tiny_add(a: int, b: int) -> int:
    """Tiny arithmetic target with a couple of obvious mutants."""
    return a + b


def medium_clamp(x: int, lo: int = 0, hi: int = 10) -> int:
    """Branchy target with a moderate mutant count."""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def heavy_score(a: int, b: int, c: int, mode: str = "sum") -> int:
    """Heavier target used to benchmark broader test selection and replay cost."""
    total = a + b
    if mode == "sum":
        total += c
    elif mode == "spread":
        total -= c
    elif mode == "weighted":
        if a < 0:
            total += 2 * c
        else:
            total -= 2 * c
    else:
        total = (a * b) - c

    if total < -10:
        return -10
    if total > 20:
        return 20
    return total
