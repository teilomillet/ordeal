# Changelog

## 0.1.9

- **Thread safety** — all shared structures (PropertyTracker, Fault activation, CoverageCollector, call counters) are lock-guarded. Safe for free-threaded Python 3.13+/3.14 (no-GIL)
- **Property mining** — `mine()` discovers properties from execution traces: monotonicity, observed bounds, length relationships, in addition to existing checks
- **Known unknowns** — `STRUCTURAL_LIMITATIONS` and `not_checked` explicitly state what ordeal cannot verify

## 0.1.6

- **Parallel exploration** — `Explorer(workers=N)` runs across multiple processes, each with a unique seed. Results aggregated: runs summed, edges unioned
- **Scaling analysis** — `ordeal.scaling` module: USL/Amdahl's Law, `benchmark()` auto-measures scaling efficiency, `analyze()` fits sigma/kappa from measurements
- **CLI** — `ordeal explore -w 4` for parallel workers
- **Config** — `workers` key in `[explorer]` section

## 0.1.4

- **Audit** — `ordeal audit` measures existing tests vs ordeal-migrated tests. Runs both suites, measures coverage via coverage.py JSON, shows verified numbers with `[verified]`/`FAILED` labels
- **Epistemic guarantees** — every measurement in audit carries its status. Wilson score confidence intervals for mined properties. Self-verification cross-checks
- **Differential testing** — `diff(fn_a, fn_b)` compares two implementations on random inputs with optional tolerance
- **Test suggestions** — audit generates actionable suggestions for uncovered lines by reading source

## 0.1.0

Initial release.

- **ChaosTest** — stateful chaos testing with auto-injected nemesis, swarm mode
- **Faults** — io, numerical, timing fault primitives + PatchFault/LambdaFault base
- **Assertions** — always/sometimes/reachable/unreachable (Antithesis model)
- **Invariants** — composable named checks (no_nan & bounded(0,1))
- **Buggify** — FoundationDB-style inline fault injection
- **QuickCheck** — @quickcheck decorator with boundary-biased generation
- **Simulate** — Clock and FileSystem for no-mock testing
- **Mutations** — AST-based mutation testing (arithmetic, comparison, negate, return_none)
- **Explorer** — coverage-guided exploration with checkpointing, energy scheduling, shrinking
- **Traces** — JSON recording, replay, delta-debugging shrinking
- **CLI** — `ordeal explore` and `ordeal replay`
- **Config** — `ordeal.toml` driven configuration
- **Plugin** — pytest integration (--chaos, --chaos-seed, @pytest.mark.chaos)
- **Integrations** — Atheris (coverage-guided fuzzing), Schemathesis (API chaos)
