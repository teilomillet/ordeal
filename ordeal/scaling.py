"""Universal Scaling Law (USL) and Amdahl's Law for parallel exploration.

The USL quantifies how throughput changes as workers increase::

    C(N) = N / [1 + sigma*(N-1) + kappa*N*(N-1)]

- **sigma** captures contention — serialized work (locks, shared I/O).
- **kappa** captures coherence — cross-worker sync cost (grows quadratically).
- When kappa=0 this reduces to Amdahl's Law.

Usage::

    from ordeal.scaling import usl, fit_usl, analyze

    # Predict throughput with 8 workers
    c = usl(8, sigma=0.05, kappa=0.002)

    # Fit from benchmark measurements
    sigma, kappa = fit_usl([(1, 1.0), (2, 1.9), (4, 3.4), (8, 5.2)])

    # Full analysis with diagnosis
    analysis = analyze([(1, 1.0), (2, 1.9), (4, 3.4), (8, 5.2)])
    print(analysis.summary())

    # Benchmark the explorer automatically
    from ordeal.scaling import benchmark
    analysis = benchmark(MyServiceChaos, target_modules=["myapp"])
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "usl.py",
    "perfcontractcase.py",
    "runmutationbenchmarktrial.py",
    "benchmarkperfcontract.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "scaling"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
