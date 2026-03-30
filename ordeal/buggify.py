"""FoundationDB-style inline fault injection.

Place ``buggify()`` calls in the code under test.  They return ``False``
in production (zero cost) and probabilistically return ``True`` during
chaos testing::

    from ordeal.buggify import buggify, buggify_value

    def process(data):
        if buggify():                           # sometimes inject a delay
            time.sleep(random.random() * 5)
        result = compute(data)
        return buggify_value(result, float('nan'))  # sometimes return NaN

**Activation:** buggify is inactive by default.  Three ways to activate:

1. ``pytest --chaos`` — the plugin calls ``activate()`` automatically
2. ``auto_configure()`` — programmatic activation
3. ``activate(probability=0.1)`` — manual, per-thread

**Thread safety:** all state is thread-local (``threading.local``).
Each thread has its own RNG, active flag, and probability.  Safe for
free-threaded Python 3.13+.

**When inactive:** ``buggify()`` always returns ``False``, ``buggify_value()``
always returns the normal value.  No overhead beyond a thread-local lookup.
"""

from __future__ import annotations

import random
import threading
from typing import TypeVar

_state = threading.local()


# -- Configuration ----------------------------------------------------------


def activate(probability: float = 0.1) -> None:
    """Enable buggify for the current thread."""
    _state.active = True
    _state.probability = probability
    if not hasattr(_state, "rng"):
        _state.rng = random.Random()


def deactivate() -> None:
    """Disable buggify for the current thread."""
    _state.active = False


def set_seed(seed: int) -> None:
    """Seed the buggify RNG for deterministic reproduction."""
    _state.rng = random.Random(seed)


def is_active() -> bool:
    """Return ``True`` if buggify is currently enabled for this thread."""
    return getattr(_state, "active", False)


# -- Public API -------------------------------------------------------------


def buggify(probability: float | None = None) -> bool:
    """Return ``True`` during chaos testing, with configurable probability.

    In production (not activated), always returns ``False``.
    Controlled by a per-thread seeded RNG for reproducibility.
    """
    if not getattr(_state, "active", False):
        return False
    p = probability if probability is not None else getattr(_state, "probability", 0.1)
    rng = getattr(_state, "rng", None) or random.Random()
    return rng.random() < p


_T = TypeVar("_T")


def buggify_value(normal: _T, faulty: _T, probability: float | None = None) -> _T:
    """Return *faulty* during chaos testing (with some probability),
    otherwise return *normal*.
    """
    if buggify(probability):
        return faulty
    return normal
