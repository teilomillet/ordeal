# Changelog

## 0.2.47

**Features:**

- wire hardening into ExplorationState + concern through explore_mutate
- LLM-enhanced mutation testing with extra_mutants, concern, and hardening

**Fixes:**

- three production friction points from real-world usage


## 0.2.45

**Features:**

- minimal docstring — AI discovers by experiment, not docs

**Docs:**

- describe what ordeal IS, not what to DO


## 0.2.44

**Features:**

- module docstring is the AI gateway — catalog() first

**Docs:**

- make all new features discoverable via catalog and CLAUDE.md


## 0.2.43

**Features:**

- StateTree — navigable exploration tree with rollback


## 0.2.42

**Features:**

- deterministic supervisor + AFL-style value mutation


## 0.2.41

**Features:**

- SOTA exploration — CMPLOG, adaptive scheduling, cross-function mining


## 0.2.39

**Features:**

- close the coverage feedback loop in mine()
- coverage-aware mining — honest about when compute helps


## 0.2.38

**Features:**

- ExplorationState — unified state space exploration


## 0.2.37

**Features:**

- wire mine() into mutate, scan_module, and metamorphic


## 0.2.36

**Features:**

- chaos_for() auto-discovers faults and invariants


## 0.2.34

**Features:**

- faults as context managers + ChaosTest works with subprocesses

**Docs:**

- document fault context managers across all references


## 0.2.31

**Features:**

- eliminate all manual registration — full auto-discovery
- auto-discover new functions in catalog() via __module__ filtering


## 0.2.29

**Features:**

- @scales_linearly decorator — assert function scales with concurrency
- subprocess_delay fault for FFI latency injection
- property report shows in pytest output without --chaos

**Fixes:**

- sometimes(warn=True) uses print for pytest visibility

**Docs:**

- update all docs for subprocess_delay, scales_linearly, warn=True

**Other:**

- test: add tests for all new features (15 new tests, 854→869)


## 0.2.28

**Features:**

- audit suggests metamorphic relations from mined properties


## 0.2.26

**Features:**

- client feedback — sometimes warn, chaos_test, report, subprocess faults
- mutation kill attribution — which tests catch which mutations
- wire expected_failures from ordeal.toml through to scan_module

**Fixes:**

- changelog generation uses python instead of sed
- add expected_failures to ScanConfig dataclass and TOML parser
- client feedback items 13-17
- address client feedback items 7-12
- address 4 client-reported bugs


## 0.2.0

**Breaking changes:**

- **Schemathesis removed** — replaced by built-in OpenAPI chaos testing engine (`ordeal.integrations.openapi`). No extra dependencies. `ordeal[api]` extra is removed; API chaos testing is now built-in.
- **`mutate()` unified entry point** — `mutate_function_and_test()` still works but `mutate()` auto-detects function vs module targets.
- **`test_fn` optional** — mutation runner auto-discovers tests via pytest when omitted.

**Major features:**

- **Built-in OpenAPI engine** — `chaos_api_test()` generates HTTP traffic from OpenAPI schemas with fault injection. Zero new dependencies.
- **`catalog()`** — runtime introspection of all faults, invariants, assertions, strategies, mutations, and integrations. Self-discovering, no hardcoded lists.
- **Mutation presets** — `"essential"` (4 ops), `"standard"` (8 ops), `"thorough"` (all 14). CLI: `--preset standard`.
- **Test stub generation** — `result.generate_test_stubs()` or `--generate-stubs tests/gaps.py`. Real parameter names and typed examples.
- **Batch mutation testing** — all mutants tested in a single pytest session instead of N sessions. Eliminates repeated startup overhead.
- **Parallel module-level mutations** — `workers=N` chunks mutants across processes, each running a batched pytest session.
- **Equivalence filtering** — `filter_equivalent=True` (default) skips mutants that produce identical outputs on random inputs.
- **Decorator unwrap** — `@ray.remote`, `@functools.wraps`, and similar decorators auto-unwrapped before `inspect.getsource()`.
- **`--chaos` in mutation runner** — ChaosTest classes and `always()`/`sometimes()` assertions are exercised during mutation scoring.
- **`NoTestsFoundError`** — raised when auto-discovery finds no tests, instead of misleading 0% score.
- **Score line** — CLI always prints `Score: X/Y (Z%)` for CI parsing. `--threshold` adds `PASS`/`FAIL`.
- **Remediation guidance** — each surviving mutant explains what test to write and why.
- **`ordeal mine` / `ordeal audit`** CLI commands for zero-config usage.
- **AI discoverability** — `llms.txt`, PyPI keywords, structured metadata in `catalog()`, AGENTS.md usage guide.

**Other:**

- Comprehensive docs overhaul, SEO, navigation tables
- Energy scheduling fix: decay 0.95→0.8, recency + exploration bonuses prevent over-exploitation
- Test coverage 43% → 74%, now 817 tests

## 0.1.30

- **Generate tests from traces** — `ordeal explore --generate-tests tests/test_gen.py` turns exploration traces into standalone pytest functions. Failures become regression tests, deep paths become coverage tests. Also available via `generate_tests()` Python API.

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
