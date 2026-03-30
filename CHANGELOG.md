# Changelog

## 0.1.19

- **Mutation score in audit** — audit now runs `validate_mined_properties()` and reports how many mutations the mined properties catch: `"mutation: 14/18 (78%)"`
- **`ordeal mine-pair`** — CLI command to discover relational properties (roundtrip, reverse, composition) between two functions: `ordeal mine-pair mymod.encode mymod.decode`
- **Shared checkpoint pool restored** — parallel workers publish high-energy checkpoints to a shared temp directory; other workers load and branch from them

## 0.1.18

- **Network faults** — `faults.network`: `http_error`, `connection_reset`, `rate_limited`, `auth_failure`, `dns_failure`, `partial_response`, `intermittent_http_error`. Duck-typed `HTTPFaultError` compatible with requests/httpx
- **Concurrency faults** — `faults.concurrency`: `contended_call`, `delayed_release`, `thread_boundary`, `stale_state`. For thread-safety and resource contention testing
- **Pydantic strategy support** — `strategy_for_type` and `@quickcheck` now derive strategies from Pydantic `BaseModel` (v2+) with constraint support (`ge`/`le`/`gt`/`lt`, `min_length`/`max_length`)

## 0.1.17

- **Schemathesis swarm mode** — `chaos_api_test(swarm=True)` and `@with_chaos(swarm=True)` pick a random fault subset per session, matching ChaosTest's swarm design
- **Mine: algebraic properties** — `mine()` now checks involution (`f(f(x)) == x`), commutativity (`f(a,b) == f(b,a)`), and associativity. Float comparisons use `math.isclose` (rel_tol=1e-9) so rounding noise doesn't cause false negatives
- **Audit grouped summary** — mined properties grouped by kind: `"commutative(add, mul), deterministic(add)"` instead of flat list
- **Violations never silent** — `always()` and `unreachable()` raise regardless of `--chaos` flag. `mute=True` parameter for known issues
- **Explorer warnings** — warns when running without `target_modules` or when 0 edges found
- **Workers auto-detect** — `workers=0` uses `os.cpu_count()` automatically

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
