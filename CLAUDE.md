# CLAUDE.md

## Project

ordeal — Automated chaos testing for Python. Fault injection, property assertions, coverage-guided exploration, and stateful testing in one library.

Built on ideas from Antithesis (deterministic exploration), FoundationDB (BUGGIFY inline faults), Jepsen (nemesis interleaving), Hypothesis (stateful property testing), Jane Street QuickCheck (boundary-biased generation), and Meta ACH (mutation validation).

## Commands

```bash
uv sync                          # install dependencies
uv run pytest                    # run all tests (~854 tests, parallel via xdist)
uv run pytest -m "not slow"      # fast loop — skip ablation tests (~15s)
uv run pytest tests/test_X.py    # single module
uv run pytest -x                 # stop on first failure
uv run pytest --chaos            # enable chaos mode in tests
uv run pytest --chaos-seed 42    # reproducible chaos
uv run ruff check .              # lint
uv run ruff format --check .     # check formatting
uv run ruff format .             # auto-format
uv run ordeal init               # bootstrap tests for untested modules (auto-detects package)
uv run ordeal init <package>     # bootstrap tests for a specific package
uv run ordeal explore            # run coverage-guided explorer (reads ordeal.toml)
uv run ordeal mutate <target>    # mutation testing (preset="standard" by default)
uv run ordeal audit <module>     # audit test coverage for a module
uv run ordeal mine <target>      # discover properties of a function
uv run ordeal replay <trace>     # replay a failure trace
uv run ordeal benchmark          # USL scaling analysis (reads ordeal.toml)
```

## Discovery

See [AGENTS.md § Discovery](AGENTS.md#discovery) for the full reference. Quick start: `from ordeal import catalog; catalog()` returns all capabilities via runtime introspection.

## Architecture

```
ordeal/
├── __init__.py         Public API + lazy exports + catalog() discovery
├── chaos.py            ChaosTest base class (extends Hypothesis RuleBasedStateMachine)
├── explore.py          Coverage-guided explorer — AFL-style edge hashing, energy scheduling
├── assertions.py       always / sometimes / reachable / unreachable (Antithesis model)
├── buggify.py          Inline fault injection (FoundationDB BUGGIFY model)
├── quickcheck.py       @quickcheck decorator with boundary-biased type-driven strategies
├── simulate.py         Deterministic simulation primitives: Clock, FileSystem
├── invariants.py       Composable invariants with & operator: no_nan & bounded(0, 1)
├── mutations.py        AST-based mutation testing with 14 operators, batch + parallel
├── mine.py             Property mining — discover what functions actually do
├── audit.py            Module audit — mutation score, property coverage, test gaps
├── auto.py             Auto-scan: smoke-test, fuzz, chaos_for entire modules
├── metamorphic.py      Metamorphic testing — relation-based property checking
├── diff.py             Differential testing — compare two implementations
├── scaling.py          USL scaling analysis — fit_usl, analyze, benchmark
├── trace.py            Trace recording, JSON serialization, replay, delta-debugging shrink
├── config.py           ordeal.toml loader with strict validation
├── cli.py              CLI: ordeal explore / mutate / audit / mine / replay / benchmark
├── plugin.py           Pytest plugin: --chaos, --chaos-seed, --buggify-prob, --mutate
├── state.py            Unified ExplorationState — what ordeal knows about your code
├── supervisor.py       DeterministicSupervisor + StateTree — reproducible exploration with rollback
├── mutagen.py          AFL-style value mutation — bit-flip loop for Python types
├── cmplog.py           Comparison logging — crack guarded branches (AFL++ CMPLOG/RedQueen)
├── strategies.py       Adversarial Hypothesis strategies for fuzzing
├── demo.py             Demo utility functions for examples and testing
├── faults/
│   ├── __init__.py     Fault / PatchFault / LambdaFault base abstractions
│   ├── io.py           error_on_call, disk_full, permission_denied, corrupt/truncate output
│   ├── numerical.py    nan_injection, inf_injection, wrong_shape, corrupted_floats
│   ├── timing.py       timeout, slow, intermittent_crash, jitter
│   ├── network.py      http_error, connection_reset, dns_failure, rate_limited
│   └── concurrency.py  contended_call, delayed_release, stale_state, thread_boundary
└── integrations/
    ├── atheris_engine.py    Coverage-guided fuzzing bridge (optional: atheris)
    └── openapi.py           Built-in API chaos testing (no extra deps)
```

## Key design decisions

- **ChaosTest** extends `hypothesis.stateful.RuleBasedStateMachine`. A **nemesis rule** is auto-injected to toggle faults during exploration. Hypothesis explores rule interleavings + fault schedules.
- **Swarm mode**: Each test run uses a random subset of faults. Better aggregate coverage than all-faults-always-on.
- **Energy scheduling**: Checkpoints that led to new edge coverage get higher selection probability. Constants: reward=2.0, decay=0.8, min=0.01. Selection combines energy, recency, and exploration bonuses to avoid over-exploiting one checkpoint. Energy propagates across workers via the shared ring buffer.
- **Parallel sharing**: Workers communicate via three shared-memory regions: (1) edge bitmap (65536 bytes, AFL-style), (2) state bitmap (65536 bytes, global state dedup), (3) ring buffer (256 slots × 16KB, checkpoint exchange with energy propagation). Per-worker slot ownership eliminates write contention. CRC32 checksums guard against torn reads.
- **Assertions**: `always`/`unreachable` raise immediately (triggers Hypothesis shrinking). `sometimes`/`reachable` are deferred — checked at session end via PropertyTracker.
- **buggify()**: No-op when chaos mode is inactive. Thread-local RNG, seed-controlled, negligible overhead in production.
- **PatchFault**: Resolves a dotted path (e.g. `"myapp.api.call"`) and wraps the target function with fault behavior. Activate/deactivate cycle managed by ChaosTest.
- **Optional deps**: atheris, numpy are behind try/except imports with helpful error messages.
- **Mutation runner**: Auto-discovers tests via pytest with `--chaos` flag (ChaosTest classes are exercised). Batch mode runs all mutants in one pytest session. Parallel mode (`workers > N`) chunks mutants across processes. Equivalence filtering skips mutants that produce identical outputs. Decorated functions (`@ray.remote`, `functools.wraps`) are auto-unwrapped before `inspect.getsource`.

## Conventions

- Python >= 3.12. Type hints throughout.
- `ruff` for lint and format. Line length 99. Rules: E, F, I, W.
- Tests in `tests/`. Test files prefixed `test_*.py`. Helper modules prefixed `_*.py`.
- Configuration via `ordeal.toml` — see `ordeal.toml.example` for full schema.
- Hypothesis stateful API (`rule`, `invariant`, `initialize`, `precondition`, `Bundle`) is re-exported from `ordeal.__init__`.
- Version derived from git tags via `setuptools-scm`.

## Dependencies

- **Required**: hypothesis >= 6.100.0, pytest >= 8.0.0
- **Optional extras**:
  - `ordeal[atheris]` — coverage-guided fuzzing via Google Atheris
  - `ordeal[all]` — everything including numpy
- **API chaos testing** is built-in (no extra install)
- **Dev**: ruff, pytest-cov, pytest-xdist (`pip install ordeal[dev]`)

## Using ordeal

Quick reference — match what the developer wants to the right tool:

- **"I have no tests yet"** → `ordeal init` (discovers inputs via mine, pins values, asserts properties, validates with mutations, generates ordeal.toml)
- **"Are my tests good enough?"** → `mutate("myapp.func", preset="standard")` + `result.kill_attribution()` for test strength analysis
- **"Fix my test gaps"** → `result.generate_test_stubs()` or `ordeal mutate --generate-stubs` (now suggests invariants by name/type)
- **"Test under failure conditions"** → `@chaos_test` decorator or `ChaosTest` with `faults = [...]`
- **"What if this call fails?"** → `buggify()`
- **"What if a subprocess fails?"** → `faults.io.subprocess_timeout("cargo run")`, `corrupt_stdout("my_binary")`, or `subprocess_delay("cargo run", delay=5.0)`
- **"Explore all reachable states"** → `ordeal explore`
- **"Deep-explore a module"** → `from ordeal.state import explore; state = explore("myapp")` (unified mine + scan + mutate + chaos, returns ExplorationState with confidence/frontier)
- **"Audit test coverage"** → `ordeal audit myapp.scoring`
- **"Discover properties"** → `ordeal mine myapp.scoring.compute`
- **"Discover properties across a module"** → `from ordeal import mine_module; mine_module("myapp")` (single-function + cross-function relations)
- **"Discover algebraic relations automatically"** → `from ordeal.metamorphic import discover_relations; discover_relations(my_fn)`
- **"Smoke-test a whole module"** → `scan_module("myapp.scoring", expected_failures=["known_broken"])`
- **"Does my refactor change behavior?"** → `diff(old_fn, new_fn)`
- **"Test algebraic relations"** → `@metamorphic(Relation(...))` or `@metamorphic()` (auto-discovers relations)
- **"Reproducible exploration"** → `from ordeal.supervisor import DeterministicSupervisor; with DeterministicSupervisor(seed=42):`
- **"Navigate the state space"** → `from ordeal.supervisor import StateTree` (checkpoint, rollback, branch, frontier)
- **"Crack guarded branches"** → `from ordeal.cmplog import extract_comparison_values` (finds magic values in AST)
- **"Mutate known-good inputs"** → `from ordeal.mutagen import mutate_inputs` (AFL-style value mutation)
- **"How does my system scale?"** → `ordeal benchmark` or `fit_usl(measurements)` or `@scales_linearly`
- **"Get a preflight report"** → `from ordeal import report; report()` (structured pass/fail summary)
- **"What can ordeal do?"** → `from ordeal import catalog; catalog()`

### Mutation testing — `mutate()`

```python
from ordeal import mutate

result = mutate("myapp.scoring.compute", preset="standard")  # auto-detects function vs module
print(result.summary())          # test gaps with cause + fix
stubs = result.generate_test_stubs()  # Python test file with real signatures
```

- `preset`: `"essential"` (4 ops, fast), `"standard"` (8, CI default), `"thorough"` (all 14).
- `test_fn` is optional — pytest auto-discovers relevant tests when omitted.
- `workers`: Parallel mutant testing. Module-level uses batched parallel (one pytest session per worker).
- `filter_equivalent`: Skip mutants that produce identical outputs on random inputs (default `True`).
- `result.survived` — list of gaps, each with `.operator`, `.source_line`, `.remediation`.
- `result.generate_test_stubs()` — test file with real param names, typed examples, and invariant suggestions (score→bounded, float→finite).
- `result.kill_attribution()` — `dict[str, list[Mutant]]` mapping test names to mutants they killed. Shows which tests carry their weight.
- `result.score` — kill ratio (0.0–1.0). CLI prints `Score: X/Y (Z%)` for CI parsing.
- `NoTestsFoundError` — raised when auto-discovery finds no tests. Message includes `generate_starter_tests()` and `ordeal init` guidance.
- Works with decorated functions: `@ray.remote`, `@functools.wraps`, etc. are auto-unwrapped.
- CLI: `uv run ordeal mutate myapp.scoring.compute --preset standard --generate-stubs tests/test_gaps.py`
- CLI: `uv run ordeal mutate myapp.scoring --workers 4 --threshold 0.8`
- Config: `[mutations]` section in `ordeal.toml` (see `ordeal.toml.example`).
- Pytest: `@pytest.mark.mutate("myapp.func", preset="standard")` with `--mutate` flag.

### "Test my code under failure conditions" / "Chaos test"

Stateful chaos testing with fault injection.

```python
from ordeal import ChaosTest, chaos_test, rule, always
from ordeal.faults import timing, io

@chaos_test  # directly pytest-discoverable, no TestCase boilerplate
class MyServiceChaos(ChaosTest):
    faults = [timing.timeout("myapp.db.query"), io.error_on_call("myapp.cache.get")]

    @rule()
    def call_service(self):
        result = my_service.process("input")
        always(result is not None, "never returns None")
```

Also works as a factory: `@chaos_test(faults=[timing.timeout("myapp.api")])`.
For auto-generated chaos tests: `chaos_for("myapp.scoring", invariants={"compute": bounded(0, 1)})`.

### "Inject faults inline" / "What if this call fails?"

Inline fault injection — no-op in production, active under `--chaos`.

```python
from ordeal import buggify
if buggify():
    data = corrupt(data)  # only runs during chaos testing
```

### "Explore all reachable states" / "Deep testing"

Coverage-guided exploration — AFL-style edge discovery.

```bash
uv run ordeal explore              # reads ordeal.toml
uv run ordeal explore -v -w 4      # verbose, 4 workers
```

### "Audit a module" / "What's the test coverage?"

Module audit — mutation score, property coverage, test gaps.

```bash
uv run ordeal audit myapp.scoring
```

### "Discover properties" / "What invariants hold?"

Property mining — automatically discovers what's true about a function.

```bash
uv run ordeal mine myapp.scoring.compute
```

### "Bootstrap tests for a new project" / `ordeal init`

One command: zero tests to validated test suite.

```bash
ordeal init                    # auto-detect package, generate everything
ordeal init myapp              # explicit target
ordeal init --dry-run          # preview without writing
```

What it does (discovery-driven, no hand-crafted values):
- `mine()` runs each function with Hypothesis random inputs, discovers (input, output) pairs + properties
- `scan_module` smoke-tests with 30 random inputs, finds crashes
- Pins diverse machine-discovered values (simplest inputs, diverse outputs)
- Asserts discovered properties via `@quickcheck` (commutativity, bounds, idempotence, involution)
- Generates `chaos_for()` stateful ChaosTest
- Validates with mutation testing (up to 3 auto-fix rounds)
- Runs 10s coverage-guided explore
- Generates `ordeal.toml` for explore/mutate/audit
- JSON to stdout for AI assistants, quality report to stderr for humans

### "Get a preflight report" / `report()`

Structured summary of all tracked `always`/`sometimes`/`reachable` assertions:

```python
from ordeal import report
r = report()  # {"passed": [...], "failed": [...]}
for p in r["failed"]:
    print(p["summary"])  # "FAIL high scores (sometimes: never true in 50 hits)"
```

### New in recent versions

- **`@chaos_test`** — decorator that replaces `TestCase = MyChaos.TestCase` boilerplate
- **`sometimes(..., warn=True)`** — visible in normal pytest without `--chaos`
- **`result.kill_attribution()`** — which tests kill which mutations
- **`subprocess_timeout(target)`** / **`corrupt_stdout(target)`** / **`subprocess_delay(target, delay=1.0)`** — subprocess/FFI fault injection
- **`Literal["a", "b"]`** — auto-generates `sampled_from` strategy
- **`scan_module(expected_failures=["broken_fn"])`** — skip known failures
- **`scan_module(max_examples={"expensive_fn": 3, "__default__": 30})`** — per-function depth
- **`chaos_for(invariants={"compute": bounded(0, 1)})`** — per-function invariants
- **Invariant `&` composition** — `bounded(0, 1) & finite` builds compound checks
- **ML array support** — invariants auto-convert MLX/JAX/PyTorch arrays via `np.asarray`
- **`fuzz().failing_args`** — shrunk input captured on failure
- **Invariant diffs** — violations show actual value, expected bound, index, deviation
- **`@scales_linearly`** — decorator that asserts a function scales linearly via USL fit
- **Property report without `--chaos`** — prints whenever there are tracked results
- **`sometimes(warn=True)` prints to stdout** — captured by pytest, includes details
- **Faults as context managers** — `with fault:` activates on enter, deactivates on exit
- **ChaosTest + subprocess faults** — subprocess_timeout/delay/corrupt_stdout work in ChaosTest when code calls subprocess.run per rule
- **`chaos_for()` auto-discovers faults and invariants** — scans AST for subprocess/file/network calls → generates faults; mines each function → generates invariants; zero config
- **`mutate()` mine-based oracle** — when no tests exist, falls back to mine() as the test oracle; zero-test mutation testing
- **`scan_module()` property checks** — reports semantic anomalies (suspicious non-universal properties), not just crashes
- **`@metamorphic()` auto-discovers relations** — when called with no arguments, mines the function to find commutative/deterministic/etc relations
- **`discover_relations(fn)`** — standalone relation discovery for metamorphic testing
- **`mine_module(module)`** — cross-function property mining: roundtrip, composition commutativity, output equivalence across all function pairs
- **`ExplorationState`** — unified state across all tools (mine, scan, mutate, chaos), JSON-serializable, tracks confidence and frontier
- **`explore(module)`** — assembles mine → scan → mutate → chaos in one pass; scales with `workers` and `max_examples`
- **CMPLOG for Python** — extracts comparison values from AST (`if x == 42`) and injects into strategies to crack guarded branches
- **Adaptive fault scheduling** — nemesis tracks per-fault energy: productive faults toggled more often (AFL++ MOpt equivalent)
- **Value-level mutation** — AFL bit-flip pattern for Python types (int, float, str, bytes, list, dict); wired into mine() Phase 2
- **Coverage-aware mining** — mine() tracks edge coverage per input, reports saturation, feeds coverage back to Hypothesis via target()
- **`DeterministicSupervisor`** — seeds all RNGs, patches time, logs state trajectory as Markov chain; same seed = same execution
- **`StateTree`** — navigable exploration tree with checkpoint, rollback, and branching; the AI navigates the state space

## Extending ordeal

### Add a new fault type

1. Add a function in the appropriate `ordeal/faults/*.py` module that returns a `PatchFault` or `LambdaFault`.
2. The function takes a `target` dotted path and fault-specific parameters.
3. Add tests in `tests/test_faults.py`.
4. Document in `docs/api-reference.md`.

### Add a new assertion

1. Add the function in `ordeal/assertions.py`.
2. If deferred (like `sometimes`), register with the global `tracker: PropertyTracker`.
3. Export from `ordeal/__init__.py` and add to `__all__`.
4. Add tests in `tests/test_assertions.py`.

### Add a new invariant

1. Add a function in `ordeal/invariants.py` that returns an `Invariant` instance.
2. Invariants compose with `&`. Numpy support is optional — guard with try/except.
3. Add tests in `tests/test_invariants.py`.

### Add a new mutation operator

1. Create an AST `NodeTransformer` subclass in `ordeal/mutations.py`.
2. Register it in the `OPERATORS` dict with a string key mapping to `(Counter, Applicator)`.
3. The counter counts mutation sites; the applicator applies the Nth mutation.
4. Add tests in `tests/test_battle.py` or a dedicated test file.
