# CLAUDE.md

## Project

ordeal — Automated chaos testing for Python. Fault injection, property assertions, coverage-guided exploration, and stateful testing in one library.

Built on ideas from Antithesis (deterministic exploration), FoundationDB (BUGGIFY inline faults), Jepsen (nemesis interleaving), Hypothesis (stateful property testing), Jane Street QuickCheck (boundary-biased generation), and Meta ACH (mutation validation).

## Commands

```bash
uv sync                          # install dependencies
uv run pytest                    # run all tests (~456 tests)
uv run pytest tests/test_X.py    # single module
uv run pytest -x                 # stop on first failure
uv run pytest --chaos            # enable chaos mode in tests
uv run pytest --chaos-seed 42    # reproducible chaos
uv run ruff check .              # lint
uv run ruff format --check .     # check formatting
uv run ruff format .             # auto-format
uv run ordeal explore            # run coverage-guided explorer (reads ordeal.toml)
uv run ordeal mutate <target>    # mutation testing (preset="standard" by default)
uv run ordeal audit <module>     # audit test coverage for a module
uv run ordeal mine <target>      # discover properties of a function
uv run ordeal replay <trace>     # replay a failure trace
```

## Architecture

```
ordeal/
├── __init__.py         Public API: ChaosTest, rule, invariant, always, sometimes, buggify, etc.
├── chaos.py            ChaosTest base class (extends Hypothesis RuleBasedStateMachine)
├── explore.py          Coverage-guided explorer — AFL-style edge hashing, energy scheduling
├── assertions.py       always / sometimes / reachable / unreachable (Antithesis model)
├── buggify.py          Inline fault injection (FoundationDB BUGGIFY model)
├── quickcheck.py       @quickcheck decorator with boundary-biased type-driven strategies
├── simulate.py         Deterministic simulation primitives: Clock, FileSystem
├── invariants.py       Composable invariants with & operator: no_nan & bounded(0, 1)
├── mutations.py        AST-based mutation testing with 14 operators
├── trace.py            Trace recording, JSON serialization, replay, delta-debugging shrink
├── config.py           ordeal.toml loader with strict validation
├── cli.py              CLI: ordeal explore / mutate / audit / mine / replay
├── plugin.py           Pytest plugin: --chaos, --chaos-seed, --buggify-prob flags
├── strategies.py       Adversarial Hypothesis strategies for fuzzing
├── faults/
│   ├── __init__.py     Fault / PatchFault / LambdaFault base abstractions
│   ├── io.py           error_on_call, disk_full, permission_denied, corrupt/truncate output
│   ├── numerical.py    nan_injection, inf_injection, wrong_shape, corrupted_floats
│   └── timing.py       timeout, slow, intermittent_crash, jitter
└── integrations/
    ├── atheris_engine.py    Coverage-guided fuzzing bridge (optional: atheris)
    └── openapi.py           Built-in API chaos testing (no extra deps)
```

## Key design decisions

- **ChaosTest** extends `hypothesis.stateful.RuleBasedStateMachine`. A **nemesis rule** is auto-injected to toggle faults during exploration. Hypothesis explores rule interleavings + fault schedules.
- **Swarm mode**: Each test run uses a random subset of faults. Better aggregate coverage than all-faults-always-on.
- **Energy scheduling**: Checkpoints that led to new edge coverage get higher selection probability. Constants: reward=2.0, decay=0.95, min=0.01.
- **Assertions**: `always`/`unreachable` raise immediately (triggers Hypothesis shrinking). `sometimes`/`reachable` are deferred — checked at session end via PropertyTracker.
- **buggify()**: No-op when chaos mode is inactive. Thread-local RNG, seed-controlled, negligible overhead in production.
- **PatchFault**: Resolves a dotted path (e.g. `"myapp.api.call"`) and wraps the target function with fault behavior. Activate/deactivate cycle managed by ChaosTest.
- **Optional deps**: atheris, numpy are behind try/except imports with helpful error messages.

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
  - `ordeal[api]` — API chaos testing via Schemathesis
  - `ordeal[all]` — everything including numpy
- **Dev**: ruff, pytest-cov (`pip install ordeal[dev]`)

## Using ordeal

Intent-based guide — match what the developer wants to the right ordeal tool.

### "Are my tests good enough?" / "Check test quality"

Mutation testing. Mutates the code and checks if tests catch the changes.

```python
from ordeal import mutate_function_and_test

result = mutate_function_and_test("myapp.scoring.compute", preset="standard")
print(result.summary())
```

- `preset="essential"` — 4 operators, fast. `"standard"` — 8 operators, CI default. `"thorough"` — all 14.
- `test_fn` is optional — pytest auto-discovers relevant tests when omitted.
- Each gap in `result.survived` has `.operator`, `.description`, `.source_line`, `.remediation`.
- CLI: `uv run ordeal mutate myapp.scoring.compute --preset standard`
- Config: `[mutations]` section in `ordeal.toml` (see `ordeal.toml.example`).

### "Test my code under failure conditions" / "Chaos test"

Stateful chaos testing with fault injection.

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, io

class MyServiceChaos(ChaosTest):
    faults = [timing.timeout("myapp.db.query"), io.error_on_call("myapp.cache.get")]

    @rule()
    def call_service(self):
        result = my_service.process("input")
        always(result is not None, "never returns None")

TestMyService = MyServiceChaos.TestCase  # run with: pytest --chaos
```

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
