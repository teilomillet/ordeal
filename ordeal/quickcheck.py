"""QuickCheck-style property testing with boundary-biased generation.

Inspired by Jane Street's QuickCheck for Core.  Three ideas:

1. **@quickcheck** — infer strategies from type hints, bias toward boundaries::

       @quickcheck
       def test_sort_idempotent(xs: list[int]):
           assert sorted(sorted(xs)) == sorted(xs)

2. **Boundary-biased strategies** — stress edges, not uniform random::

       from ordeal.quickcheck import biased
       biased.integers(0, 100)   # more values near 0, 1, 99, 100
       biased.floats(0.0, 1.0)   # more values near 0.0, 0.5, 1.0

3. **Type-driven generation** — strategies from type hints + dataclasses::

       from ordeal.quickcheck import strategy_for_type
       gen = strategy_for_type(MyDataclass)

The difference from raw Hypothesis: ordeal biases toward boundary values
by default.  Integers cluster near 0 and range endpoints.  Lists are more
often empty or singleton.  Strings hit unicode edge cases.  This catches
more bugs per test run because implementation boundaries (off-by-one, empty
input, overflow) are explored with higher probability.

For **hand-curated** adversarial values (SQL injection strings, NaN floats,
type-confusion), see :mod:`ordeal.strategies` instead. The two modules are
complementary: ``biased`` infers boundaries from types, ``strategies``
provides explicit chaos data for specific attack surfaces.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "biased.py",
    "applypydanticconstraints.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "quickcheck"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
