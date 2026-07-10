"""Named, composable invariant assertions.

Invariants are reusable checks with clear failure messages.
They compose with the ``&`` operator to build complex checks from
simple building blocks::

    from ordeal.invariants import no_nan, no_inf, bounded, finite

    # Compose invariants with &
    valid_score = bounded(0, 1) & finite      # bounded + no NaN + no Inf
    valid_score(model_output)                  # raises AssertionError on violation
    valid_score(model_output, name="score")    # custom name in message

    # The & operator is associative — chain as many as you like
    strict = bounded(0, 1) & finite & monotonic(strict=True)

    # Built-in `finite` is itself a composition: no_nan & no_inf
    finite(model_output)  # rejects NaN and Inf in one call

Array-like values from MLX, JAX, and PyTorch are auto-converted to
numpy arrays before checking. Numpy is optional — invariants that
only need scalars or sequences work without it.

Discover all available invariants programmatically::

    from ordeal.invariants import catalog
    for entry in catalog():
        print(f"{entry['name']}  -- {entry['doc']}")
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "invariant.py",
    "unitnormalized.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "invariants"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
