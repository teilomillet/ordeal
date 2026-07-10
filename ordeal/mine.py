"""Property mining — discover invariants from execution traces.

Run a function many times with random inputs, observe patterns in
outputs, and report likely properties.  The user confirms which are
real — turning observed regularities into tested invariants::

    from ordeal.mine import mine

    properties = mine(my_function, max_examples=500)
    for p in properties:
        print(p)
    # output >= 0: 100% (500/500)
    # output is float: 100% (500/500)
    # deterministic: 100% (500/500)
    # output in [0, 1]: 98% (490/500)
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "approxequal.py",
    "minemoduleresult.py",
    "checksorted.py",
    "minepair.py",
    "minemodule.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "mine"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
