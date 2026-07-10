"""Equivalent mutant detection — structural, statistical, and formal methods.

The equivalent mutant problem is one of the hardest open problems in mutation
testing.  An **equivalent mutant** is a code change that does not alter program
behavior under any input.  Because it can never be killed, it inflates the
denominator of the mutation score, making test suites appear weaker than they
are and creating false "test gaps" that waste developer time.

Why it is hard:

    Deciding whether two programs are semantically identical is undecidable in
    general (Rice's theorem).  Every practical approach trades off speed,
    coverage, and soundness.  No single technique works for all mutants.

This module provides three complementary approaches, ordered from fast and
conservative to slow and definitive:

1. **Structural equivalence** (``structural_equivalence``):
   Normalizes both ASTs — strips comments, canonicalizes variable names,
   folds constants — and checks structural identity.  Runs in microseconds.
   Only catches trivially equivalent mutants (e.g., ``x + 0``, reordered
   commutative operands).  Sound: if it says equivalent, it is.  Incomplete:
   many equivalent mutants have structurally different ASTs.

2. **Statistical equivalence** (``statistical_equivalence``):
   Runs both functions on boundary values and random type-driven inputs,
   then applies a Wilson score confidence interval to bound the probability
   that the functions differ.  Medium speed (milliseconds to seconds).
   Probabilistic: a high confidence of equivalence is strong evidence but
   not proof.  This extends the behavioral filtering already in
   ``ordeal.mutations._is_runtime_equivalent`` with rigorous statistics.

3. **Formal equivalence** (``prove_equivalent``):
   Encodes both functions as SMT formulas via Z3 and checks satisfiability
   of ``f(x) != g(x)``.  If UNSAT, the functions are proven identical for
   all inputs.  If SAT, the solver provides a concrete counterexample.
   Definitive when it succeeds, but may time out on complex functions.
   Requires ``pip install z3-solver`` — gracefully degrades without it.

How the three complement each other:

    Structural catches the easy cases in microseconds, so the statistical
    layer never wastes time on them.  Statistical catches most remaining
    equivalences with high confidence, filtering the candidate set for the
    expensive formal check.  Formal provides proof for the ambiguous cases
    that statistics cannot resolve.  Together, they form a layered filter
    that is both fast in practice and as rigorous as available tools allow.

The ``classify_mutant`` function runs all three in order (fast to slow),
returning the first definitive result.  ``filter_equivalent_mutants``
provides a drop-in replacement for the existing equivalence filter in
``ordeal.mutations``.

References:

- Papadakis et al., "Mutation Testing Advances: An Analysis and Survey",
  Advances in Computers, 2019 — comprehensive survey of equivalence detection.
- ICST Mutation Workshop (2023-2025) — ongoing research into scalable
  equivalence detection combining static analysis and SMT solvers.
- Meta ACH (Automated Chaos and Hardening) — industrial-scale mutation
  testing where equivalent mutant filtering is critical for signal-to-noise.
- Offutt & Pan, "Automatically Detecting Equivalent Mutants and Infeasible
  Paths", Software Testing, Verification & Reliability, 1997 — foundational
  constraint-based equivalence detection.

Z3 is optional::

    pip install z3-solver

Every function in this module works without Z3, returning inconclusive
results when formal proof would be required.

Usage::

    from ordeal.equivalence import classify_mutant, filter_equivalent_mutants

    # Classify a single mutant
    result = classify_mutant(original_fn, mutant_fn, orig_src, mut_src)
    if result.equivalent:
        print("Skip — equivalent mutant")

    # Drop-in filter for mutation testing pipeline
    surviving = filter_equivalent_mutants("myapp.scoring", mutant_pairs)
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "checkz3.py",
    "z3encodeexpr.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "equivalence"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
