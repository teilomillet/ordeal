"""Grammar-based / structure-aware input generation for structured data types.

Random testing with ``st.text()`` or ``st.binary()`` produces syntactically
invalid inputs that get rejected by the parser layer and never reach the
interesting business logic underneath.  Grammar-aware generation produces
**syntactically valid** inputs with **semantically interesting** variations,
so the fuzzer spends its cycles testing real code paths.

This is the Python equivalent of libFuzzer's structure-aware custom mutators
(``LLVMFuzzerCustomMutator``).  Where libFuzzer requires hand-written C++
mutators, ordeal provides composable Hypothesis strategies that generate
valid structured data out of the box.

How it complements the rest of ordeal:

- **CMPLOG** (:mod:`ordeal.cmplog`) cracks guarded branches on existing
  parameters by extracting literal comparison values from source code.
  Grammar strategies solve the *prior* problem: getting a well-formed input
  past the parser so those guarded branches are even reachable.
- **Adversarial strategies** (:mod:`ordeal.strategies`) inject known-bad
  values (SQL injection, NaN floats).  Grammar strategies inject
  *structurally valid* values with adversarial variation (deep nesting,
  edge-case keys, unusual but legal characters).
- **Coverage-guided exploration** (:mod:`ordeal.explore`) benefits directly:
  valid inputs produce longer execution traces with more edge diversity,
  feeding the AFL-style energy scheduling loop.

Each function returns a ``hypothesis.strategies.SearchStrategy`` suitable for
``@given()``, ``@rule()``, ``data.draw()``, or any Hypothesis context.

Discover all grammar strategies programmatically::

    from ordeal.grammar import catalog
    for entry in catalog():
        print(f"{entry['name']}  -- {entry['doc']}")
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "jsonstrategy.py",
    "xmlstrategy.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "grammar"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
