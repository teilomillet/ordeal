# AGENTS.md

## Using ordeal on a project

When a user asks you to run ordeal on their code — not to develop ordeal itself.

**What ordeal does:** Finds bugs your tests miss by exploring thousands of scenarios with realistic failures (timeouts, corrupted data, crashes). When something breaks, it shows the shortest sequence that reproduces the failure.

**Start here — bootstrap tests for any project:**

```bash
ordeal init                      # auto-detect package, generate tests, validate with mutations
ordeal init mypackage            # explicit target
ordeal init --dry-run            # preview without writing
```

This generates tests with pinned values (machine-discovered via mine/Hypothesis), property assertions, a stateful ChaosTest, and an ordeal.toml — then validates with mutation testing.

**Other commands:**

```bash
ordeal mine mymodule            # discover what functions actually do
ordeal audit mymodule            # find gaps in existing tests
ordeal explore                   # coverage-guided exploration (reads ordeal.toml)
ordeal replay trace.json         # reproduce a specific failure
ordeal mutate mymodule.func      # verify tests catch real code changes
ordeal mutate mymodule --workers 4 --threshold 0.8  # parallel, CI gate
```

**Mutation testing — speed and accuracy:**

```python
from ordeal import mutate

# Auto-discovers tests, runs with --chaos, filters equivalent mutants
result = mutate("mymodule.func", preset="standard")

# Parallel: batches mutants into one pytest session per worker
result = mutate("mymodule", preset="standard", workers=4)

# CI: check score and generate stubs for gaps
print(result.score)              # 0.83
print(result.summary())          # gaps with cause + fix guidance
stubs = result.generate_test_stubs()  # test file for surviving mutants
```

- `workers=N` — parallel batch mode. Each worker runs one pytest session testing N/workers mutants. Much faster than serial.
- `filter_equivalent=True` (default) — skips mutants that produce identical outputs on random inputs.
- Works with `@ray.remote`, `@functools.wraps`, and other decorators (auto-unwrapped).
- Runs pytest with `--chaos` so ChaosTest assertions count toward the score.
- Raises `NoTestsFoundError` if no tests match (suggests `ordeal init` and `generate_starter_tests()`).
- `result.kill_attribution()` — which tests killed which mutants (test strength analysis).
- `generate_test_stubs()` now suggests invariants by name/type (score→bounded, float→finite).

**New features:**

- `@chaos_test` decorator — no `TestCase = MyChaos.TestCase` boilerplate
- `sometimes(cond, name, warn=True)` — visible in normal pytest without `--chaos`
- `report()` — structured pass/fail summary of all property assertions
- `subprocess_timeout("cargo run")` / `corrupt_stdout("binary")` — subprocess/FFI faults
- `Literal["a", "b"]` types auto-generate `sampled_from` strategies
- `scan_module(expected_failures=["known_broken"])` — skip known failures
- `scan_module(max_examples={"expensive_fn": 3, "__default__": 30})` — per-function depth
- `chaos_for(invariants={"compute": bounded(0, 1)})` — per-function invariants
- Invariant `&` composition: `bounded(0, 1) & finite` — user-facing API
- ML arrays (MLX/JAX/PyTorch) auto-converted in invariants and registered as strategies
- `fuzz().failing_args` — shrunk failing input captured
- Invariant violations show actual value, expected bound, index, and deviation

**Reading output:**
- `ALWAYS property (N/N)` — held every time across all explored scenarios.
- `X% property (M/N)` — failed in some cases. Fix the failing ones.
- `SURVIVED L42:8 + -> -` — mutation your tests didn't catch. Gap at that line.
- `Score: X/Y (Z%)` — final score line, always printed for CI parsing.
- `suggest: L42 test when x < 0` — actionable suggestion.

**Discover everything available programmatically:**

```python
from ordeal import catalog
c = catalog()
# 12 subsystems: faults, invariants, assertions, strategies, mutations,
# integrations, mining, audit, auto, metamorphic, diff, scaling
# Each entry has: name, qualname, signature, doc
```

**Deeper understanding:** https://docs.byordeal.com/ — conceptual explanations (in highlighted blocks) before each technical section.

---

## Developing ordeal

Conventions for AI agents working on ordeal.

## Build & test

```bash
uv sync                          # install deps
uv run pytest                    # run all tests (parallel via xdist, ~40s)
uv run pytest -m "not slow"      # fast loop — skip ablation tests (~15s)
uv run pytest --chaos            # with property reporting
uv run ordeal explore -c demo.toml  # run explorer (needs PYTHONPATH if tests/ is target)
```

## Project structure

- `ordeal/` — library source (modules + faults/ + integrations/)
- `tests/` — test files, includes `test_battle.py` (ordeal testing itself)
- `docs/` — markdown docs, `index.md` is the hub
- `ordeal.toml.example` — annotated config reference

## Rules

- Python 3.12+. Use `match/case`, `type | None` syntax, `from __future__ import annotations`.
- Every public function has a docstring. Every parameter is typed. No untyped `Any` except at system boundaries (wrapping unknown functions, JSON codecs, optional deps).
- Tests go in `tests/test_<module>.py`. Battle tests (ordeal testing itself) go in `tests/test_battle.py`.
- Faults are in `ordeal/faults/{io,numerical,timing,network,concurrency}.py`. New fault types go in the matching file or a new one.
- No emojis in code or docs.
- Keep docs under 130 lines each. Example-first, minimal prose.

## Design principle: simple core, depth through parameters

Every function must be simple to call by default. Complexity is unlocked through optional parameters on the SAME function — never by adding a second function.

**Do this:**
```python
sometimes(condition, "name")                              # simple, deferred
sometimes(lambda: fn(), "name", attempts=100)             # depth: immediate retry
```

**Not this:**
```python
sometimes(condition, "name")           # one function for simple case
check_sometimes(fn, "name", attempts=100)  # separate function for advanced case
```

The rule: if you're about to add a new function that does "the same thing but more", add a parameter to the existing function instead. One name, one import, discoverable depth.

This applies everywhere: faults, assertions, invariants, strategies. The user should never need to learn a second API to do more with the same concept.

## Architecture decisions

- `ChaosTest` extends Hypothesis's `RuleBasedStateMachine`. The nemesis rule is auto-injected.
- The Explorer is separate from Hypothesis — it drives rules manually with coverage feedback.
- `buggify()` is a no-op when inactive (thread-local `_state.active` check, negligible overhead).
- Assertions use a global `PropertyTracker` — thread-safe, activated by `--chaos` flag or `auto_configure()`.
- TOML config (`ordeal.toml`) is the interface between humans/agents and the Explorer.
- Traces are JSON. Replay uses recorded param values, not re-drawing from strategies.
- Parallel workers share three shared-memory regions: edge bitmap (AFL-style), state bitmap (global dedup), ring buffer (checkpoint exchange with energy propagation). Per-worker slot ownership, CRC32 integrity, no locks.
- New capabilities (mine, audit, auto, metamorphic, diff, scaling) are lazy-imported via `__getattr__` to keep `import ordeal` fast.

## When adding a new feature

1. Write the module in `ordeal/`.
2. Add tests in `tests/test_<module>.py`.
3. Add a battle test in `tests/test_battle.py` if the feature has interesting state.
4. Export from `ordeal/__init__.py` if it's public API.
5. Add a doc in `docs/` (under 130 lines).
6. Update `docs/api-reference.md`.
