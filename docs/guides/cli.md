---
description: >-
  Ordeal CLI: explore, replay, mine, audit, mutate. Run chaos testing,
  mutation testing, and property mining from the command line. Full
  command reference.
---

# CLI

!!! quote "In plain English"
    The CLI is how you run ordeal outside of pytest. It gives you three superpowers: `explore` to find bugs, `replay` to reproduce them, and tools like `mine`, `audit`, and `benchmark` to understand your code and your tests. The typical workflow is: explore, find a failure, replay it, fix the bug.

## Install

```bash
uv tool install ordeal     # global, `ordeal` on PATH
uvx ordeal explore         # ephemeral, no install
uv run ordeal explore      # inside project venv
```

## Commands

### `ordeal mutate`

Run mutation testing from the command line. Auto-discovers tests via pytest, runs them with `--chaos` enabled so ChaosTest assertions count toward the mutation score.

```bash
ordeal mutate myapp.scoring.compute                          # single function
ordeal mutate myapp.scoring                                  # whole module
ordeal mutate myapp.scoring --preset thorough --workers 4    # parallel, all operators
ordeal mutate myapp.scoring --threshold 0.8                  # fail if score < 80%
ordeal mutate myapp.scoring --generate-stubs tests/gaps.py   # write test stubs
```

Output always includes a `Score:` line for CI parsing:

```
Mutation score: 15/18 (83%)  [target: myapp.scoring.compute, preset: standard]
  3 test gap(s) — each is a code change your tests fail to catch:
  GAP L42:8 [arithmetic] + -> -  |  return a + b
    Fix: Add an assertion that checks the exact numeric result...
Score: 15/18 (83%)
Threshold: 80% — PASS
```

| Flag | Default | Description |
|---|---|---|
| `targets` | required | Dotted paths (positional, one or more) |
| `--preset` | `standard` | `"essential"`, `"standard"`, or `"thorough"` |
| `--workers` | `1` | Parallel workers (batched pytest sessions per worker) |
| `--threshold` | `0.0` | Minimum score; exit code 1 if below |
| `--generate-stubs` | — | Write test stubs for surviving mutants to this path |
| `--no-filter` | off | Disable equivalence filtering |
| `--equivalence-samples` | `10` | Random inputs for equivalence check |

### `ordeal mine-pair`

!!! quote "What this unlocks"
    If you have two functions that should be inverses of each other -- like `encode`/`decode` or `serialize`/`parse` -- this command automatically checks whether that's actually true. No test code needed. Just point it at two functions and it tells you if the roundtrip holds.

Discover relational properties between two functions — roundtrip (`g(f(x)) == x`), reverse roundtrip, and commutative composition:

```bash
ordeal mine-pair mymod.encode mymod.decode
ordeal mine-pair json.dumps json.loads -n 500
```

```
mine_pair(encode, decode): 200 examples
  ALWAYS  roundtrip g(f(x)) == x (200/200)
  ALWAYS  roundtrip f(g(x)) == x (200/200)
```

| Flag | Default | Description |
|---|---|---|
| `f` | required | First function (dotted path) |
| `g` | required | Second function (dotted path) |
| `--max-examples`, `-n` | `200` | Examples to sample |

### `ordeal audit`

!!! quote "Why this matters"
    Audit answers the question: "are my tests actually good?" It measures your existing tests, generates ordeal-style replacements, and compares them side by side. Every number is verified, not estimated. If ordeal can match your coverage with less code, you know where your tests have unnecessary complexity.

Measure your existing tests vs what ordeal auto-scan achieves — verified numbers, not estimates:

```bash
ordeal audit myapp.scoring --test-dir tests/
ordeal audit myapp.scoring myapp.pipeline -t tests/ --max-examples 50
ordeal audit myapp.scoring --validation-mode deep
```

Output:

```
ordeal audit

  myapp.scoring
    current:   33 tests |   343 lines | 98% coverage [verified]
    migrated:  12 tests |   130 lines | 96% coverage [verified]
    saving:   64% fewer tests | 62% less code | same coverage
    mined:    deterministic(compute, normalize), output in [0, 1](compute)
    mutation: 14/18 (78%)
    suggest:
      - L42 in compute(): test when x < 0
      - L67 in normalize(): test that ValueError is raised
```

Every number is `[verified]` (measured and cross-checked for consistency) or `FAILED: reason`. When `pytest-cov` is installed, ordeal uses its JSON report; otherwise it falls back to an internal tracer. Mined properties are grouped by kind. The mutation score shows how many code mutations the mined properties catch — if it's below 100%, the surviving mutants reveal property gaps.

`--validation-mode fast` replays mined inputs against each mutant and is the default because it is much faster. `--validation-mode deep` keeps that replay check and then re-runs `mine()` on each mutant, which is slower but keeps the broader exploratory search.

The "migrated" column shows what a real ordeal test file looks like: `fuzz()` for crash safety plus explicitly mined properties (bounds, determinism, type checks). It generates the test file a developer would write after adopting ordeal.

Use `--show-generated` to inspect the generated test, or `--save-generated` to save it and use it directly:

```bash
ordeal audit myapp.scoring --show-generated          # print generated test
ordeal audit myapp.scoring --save-generated test_migrated.py  # save to file
```

| Flag | Default | Description |
|---|---|---|
| `modules` | required | Module paths to audit (positional, one or more) |
| `--test-dir`, `-t` | `tests` | Directory containing existing tests |
| `--max-examples` | `20` | Hypothesis examples per function |
| `--validation-mode` | `fast` | `fast` replay or `deep` replay + re-mine for mutation validation |
| `--show-generated` | off | Print the generated test file |
| `--save-generated` | — | Save generated test to this path |

### `ordeal mine`

!!! quote "Think of it this way"
    Instead of you guessing what properties a function has, `mine` discovers them automatically. It runs the function hundreds of times with random inputs and tells you what's always true: "output is always a float," "always between 0 and 1," "always deterministic." These discovered properties become your test assertions.

Discover properties of a function or all public functions in a module. Prints what mine() finds — type invariants, algebraic laws, bounds, monotonicity, length relationships — with confidence levels.

```bash
ordeal mine myapp.scoring.compute           # single function
ordeal mine myapp.scoring                   # all public functions
ordeal mine myapp.scoring.compute -n 1000   # more examples = tighter confidence
```

Output:

```
mine(compute): 500 examples
  ALWAYS  output type is float (500/500)
  ALWAYS  deterministic (50/50)
  ALWAYS  output in [0, 1] (500/500)
  ALWAYS  observed range [0.0, 0.9987] (500/500)
  ALWAYS  monotonically non-decreasing (499/499)
    n/a: commutative, associative
```

Use this to understand a function before writing tests. The `ALWAYS` properties are candidates for assertions; the `n/a` list shows what doesn't apply. `result.not_checked` (visible in the Python API) lists what mine() structurally cannot verify — those are the tests you write manually.

| Flag | Default | Description |
|---|---|---|
| `target` | required | Dotted path: `mymod.func` or `mymod` (positional) |
| `--max-examples`, `-n` | `500` | Examples to sample |

### `ordeal mine-pair`

Discover relational properties between two functions: roundtrip (`g(f(x)) == x`), reverse roundtrip (`f(g(x)) == x`), and commutative composition (`f(g(x)) == g(f(x))`).

```bash
ordeal mine-pair myapp.encode myapp.decode           # roundtrip?
ordeal mine-pair myapp.serialize myapp.parse -n 500  # more examples
```

Output:

```
mine(encode <-> decode): 200 examples
  ALWAYS  roundtrip decode(encode(x)) == x (48/48)
  ALWAYS  roundtrip encode(decode(x)) == x (45/45)
     52%  commutative composition (26/50)
```

Use this when you have function pairs that should be inverses (encode/decode, serialize/parse, compress/decompress) or that should commute.

| Flag | Default | Description |
|---|---|---|
| `f` | required | First function (positional) |
| `g` | required | Second function (positional) |
| `--max-examples`, `-n` | `200` | Examples to sample |

### `ordeal benchmark`

!!! quote "What you can do with this"
    Before you set `workers = 8` in your config, run `benchmark` to find out if 8 workers actually helps. Some tests hit diminishing returns at 4 workers, others scale to 16. This command measures real throughput and tells you the sweet spot for your specific test and machine.

Measure how parallel exploration scales on your machine and test class. Runs the Explorer at N=1, 2, 4, 8... workers, measures throughput, and fits the Universal Scaling Law (USL):

```bash
ordeal benchmark                          # uses ordeal.toml, first [[tests]] entry
ordeal benchmark -c ci.toml               # custom config
ordeal benchmark --max-workers 16         # test up to 16 workers
ordeal benchmark --time 30                # 30s per trial (default: 10s)
ordeal benchmark --metric edges           # fit on edges/sec instead of runs/sec
ordeal benchmark --perf-contract ordeal.perf.toml --check
```

```
Scaling Analysis (Universal Scaling Law)
  sigma (contention):  0.080755
  kappa (coherence):   0.005578
  Regime:              usl
  Optimal workers:     13.4
  Peak throughput:     7.64x

  Diagnosis:
    Contention (sigma): 8.1% serialized fraction.
    Coherence (kappa):  0.005578 cross-worker sync cost.
```

For checked-in benchmark contracts, `--perf-contract` enforces both latency and audit-quality drift in CI:

```toml
[[cases]]
name = "audit_demo_fast_vs_deep"
kind = "audit_compare"
module = "ordeal.demo"
validation_mode = "fast"
compare_validation_mode = "deep"
max_score_gap = 0.10
```

That case fails if fast audit validation falls more than 10 percentage points behind deep validation on mutation score.

| Flag | Default | Description |
|---|---|---|
| `--config`, `-c` | `ordeal.toml` | Config file |
| `--max-workers` | CPU count | Maximum workers to test |
| `--time` | `10` | Seconds per trial |
| `--metric` | `runs` | `"runs"` (runs/sec) or `"edges"` (edges/sec) |
| `--perf-contract` | — | Run a checked-in perf/quality contract instead of scaling analysis |
| `--check` | off | Exit with code 1 if any contract case exceeds its budget |

### `ordeal explore`

!!! quote "The key insight"
    This is the core of ordeal. It reads your config, loads your ChaosTest classes, and systematically explores what happens when things go wrong -- different rule orderings, different fault combinations, different timings. It's like having a tireless QA engineer who tries thousands of scenarios while you write code.

Your main command for deep exploration. Reads `ordeal.toml`, loads each ChaosTest class, and runs coverage-guided exploration with fault injection, energy scheduling, and swarm mode.

Use for: pre-commit validation, pre-release exploration runs, CI pipelines, and finding deep bugs that unit tests miss.

```bash
ordeal explore                          # reads ordeal.toml
ordeal explore -c ci.toml              # custom config
ordeal explore -v                       # live progress
ordeal explore --max-time 300          # override time
ordeal explore --seed 99               # override seed
ordeal explore --no-shrink             # skip failure minimization
ordeal explore -w 4                    # 4 parallel workers
ordeal explore --generate-tests tests/test_generated.py  # turn traces into pytest tests
```

The `--workers` / `-w` flag runs exploration across multiple processes. Each worker gets a unique seed for independent state-space exploration. Results are aggregated: runs/steps are summed, edges are unioned for true unique count. Use `--workers $(nproc)` for full CPU utilization.

### `ordeal replay`

!!! quote "How to explore this"
    When `explore` finds a bug, it saves a trace -- the exact sequence of steps that triggered the failure. `replay` re-runs those steps so you can see the bug happen again. Use `--shrink` to strip the trace down to the minimum steps needed, which makes the bug much easier to understand.

Reproduce a failure from a saved trace. The trace file contains the exact sequence of rules and fault toggles that triggered the failure, so replaying it re-executes the same steps.

Use for: triaging a CI failure, sharing a reproducible bug with a colleague, verifying that a fix actually resolves the issue.

```bash
ordeal replay .ordeal/traces/fail-run-42.json          # reproduce
ordeal replay --shrink trace.json                       # minimize
ordeal replay --shrink trace.json -o minimal.json      # save minimized
```

The `--shrink` flag runs delta-debugging to remove unnecessary steps from the trace. Use it when: the trace is too long to understand, or you want the minimal sequence of operations that reproduces the failure. The shrunk trace is often 5-10x shorter than the original.

## Workflows

!!! quote "In plain English"
    These workflows show how ordeal fits into your daily development cycle. The pattern is simple: explore fast while coding, explore thoroughly in CI, and when something fails, replay and shrink the trace until you understand the bug.

### Local development

Quick exploration with live progress. Run this before committing to catch obvious issues:

```bash
ordeal explore -v --max-time 30
```

The `-v` flag prints a progress line showing runs, steps, edges discovered, and failures found. Thirty seconds is enough to catch most shallow bugs.

### CI pipeline

Longer exploration with a dedicated config, JSON report, and a nonzero exit code on failure:

```bash
ordeal explore -c ci.toml
```

Where `ci.toml` might set `max_time = 120`, `report.format = "json"`, and `report.output = "ordeal-report.json"`. The exit code is 1 if any failure is found, so your CI script can gate on it directly.

### Bug triage

When a CI run or colleague reports a failure trace:

```bash
ordeal replay trace.json                          # confirm it reproduces
ordeal replay --shrink trace.json -o minimal.json # minimize it
```

The shrunk trace gives you the shortest sequence of operations that triggers the bug. Read through the steps: which rules ran, which faults were active, and where the exception occurred.

### Reproducibility

Fix the seed for deterministic exploration. The same seed produces the same sequence of rule interleavings and fault schedules:

```bash
ordeal explore --seed 42
```

Useful for: bisecting changes (did this commit introduce the failure?), comparing exploration runs across branches, and ensuring consistent CI behavior.

## pytest integration

!!! quote "Think of it this way"
    You don't have to choose between pytest and the ordeal CLI -- they work together. pytest is great when you want chaos testing mixed into your regular test suite. The CLI is great for standalone exploration runs. Most teams use both: pytest with `--chaos` in CI, and `ordeal explore` for deeper pre-release validation.

ordeal also works as a pytest plugin (auto-registered when ordeal is installed). No configuration needed -- pytest picks it up automatically via the `pytest11` entry point.

### How `--chaos` works

```bash
pytest --chaos                    # enable chaos mode
pytest --chaos --chaos-seed 42    # reproducible seed
pytest --chaos --buggify-prob 0.2 # higher fault probability
```

When you pass `--chaos`, three things happen:

1. **PropertyTracker activates**: all `always()`, `sometimes()`, `reachable()`, and `unreachable()` calls start recording hits and results instead of being no-ops.
2. **buggify() activates**: every `buggify()` call in your code has a chance of returning True (default 10%, controlled by `--buggify-prob`).
3. **Chaos-only tests run**: tests marked with `@pytest.mark.chaos` are collected instead of skipped.

Without `--chaos`, your test suite runs normally. buggify() returns False, assertions are no-ops, and chaos-marked tests are skipped.

### `@pytest.mark.chaos`

Mark tests that should only run under chaos mode. These are skipped without the `--chaos` flag, so your normal CI runs are not affected:

```python
import pytest

@pytest.mark.chaos
def test_under_chaos():
    ...
```

This is useful for tests that are slow (because they explore fault interleavings), flaky by design (because faults cause nondeterminism), or only meaningful under fault injection.

### The property report

Ordeal prints a property report at the end of the test run whenever there are tracked results — with or without `--chaos`. It shows every tracked property, its type, hit count, and pass/fail status:

```
--- Ordeal Property Results ---
  PASS  cache hit (sometimes: 47 hits)
  PASS  no data loss (always: 312 hits)
  FAIL  stale read (sometimes: never true in 200 hits)

  1/3 properties FAILED
```

`always` properties pass if they held every time they were evaluated. `sometimes` properties pass if they held at least once. `reachable` properties pass if the code path was reached. `unreachable` properties pass if it was never reached.

### `chaos_enabled` fixture

For tests that need chaos in a specific scope without requiring the global `--chaos` flag:

```python
def test_something(chaos_enabled):
    # buggify() is active, PropertyTracker is recording
    result = my_function()
    assert result is not None
```

The fixture activates buggify and the PropertyTracker for the duration of the test, then restores the previous state.

### Pytest patterns

**Pattern 1: Separate chaos tests from unit tests.** Keep chaos tests in their own directory so you can run them independently:

```
tests/
├── unit/              # fast, deterministic — always run
│   └── test_scoring.py
├── chaos/             # slower, exploratory — run with --chaos
│   └── test_scoring_chaos.py
└── conftest.py
```

```bash
pytest tests/unit/                          # fast CI gate
pytest tests/chaos/ --chaos --chaos-seed 0  # thorough validation
```

**Pattern 2: Use `chaos_enabled` for targeted chaos in unit tests.** You don't need `--chaos` for everything. Use the fixture when a specific test needs fault injection:

```python
def test_retry_logic(chaos_enabled):
    """This test specifically checks retry behavior under buggify."""
    from ordeal.buggify import buggify
    # buggify() is now active — it will sometimes return True
    result = service_with_retries.call()
    assert result is not None  # should succeed despite faults
```

**Pattern 3: Combine `@pytest.mark.chaos` with `ChaosTest.TestCase`.** ChaosTest classes work with or without `--chaos`, but marking them ensures they're skipped in fast CI runs:

```python
import pytest
from ordeal import ChaosTest, rule, always

@pytest.mark.chaos
class ScoreServiceChaos(ChaosTest):
    faults = [...]
    @rule()
    def score(self): ...

TestScoreServiceChaos = ScoreServiceChaos.TestCase
```

**Pattern 4: Auto-scan via ordeal.toml.** When you add `[[scan]]` entries to `ordeal.toml`, pytest auto-discovers and runs them. No test files needed:

```toml
# ordeal.toml
[[scan]]
module = "myapp.scoring"
max_examples = 100
```

```bash
pytest ordeal.toml --chaos  # auto-scans myapp.scoring
```

Each public function in the module becomes a test item. Functions without type hints are skipped unless fixtures are provided in the TOML.

**Pattern 5: Different buggify probabilities for different environments.**

```bash
pytest --chaos --buggify-prob 0.05   # gentle: 5% fault rate (local dev)
pytest --chaos --buggify-prob 0.1    # moderate: 10% (default, CI)
pytest --chaos --buggify-prob 0.3    # aggressive: 30% (pre-release stress)
```

Higher probability = more faults per run = finds more bugs but also more noise. Start gentle, increase as your error handling matures.

## Exit codes

`ordeal explore` returns **0** on success (no failures found) and **1** if any failure is found or if there is a configuration error. Use this directly in CI scripts:

```bash
ordeal explore -c ci.toml || exit 1
```

`ordeal replay` returns **0** if the failure did not reproduce (which can happen if the code has changed) and **1** if the failure reproduced.
