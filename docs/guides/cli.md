# CLI

## Install

```bash
uv tool install ordeal     # global, `ordeal` on PATH
uvx ordeal explore         # ephemeral, no install
uv run ordeal explore      # inside project venv
```

## Commands

### `ordeal audit`

Measure your existing tests vs what ordeal auto-scan achieves — verified numbers, not estimates:

```bash
ordeal audit myapp.scoring --test-dir tests/
ordeal audit myapp.scoring myapp.pipeline -t tests/ --max-examples 50
```

Output:

```
ordeal audit

  myapp.scoring
    current:   33 tests |   343 lines | 98% coverage [verified]
    migrated:  12 tests |   130 lines | 96% coverage [verified]
    saving:   64% fewer tests | 62% less code | same coverage
    mined:    compute: output in [0, 1] (500/500, >=99% CI)
    suggest:
      - L42 in compute(): test when x < 0
      - L67 in normalize(): test that ValueError is raised
```

Every number is `[verified]` (measured via coverage.py JSON, cross-checked for consistency) or `FAILED: reason`. Mined properties include Wilson score confidence intervals.

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
| `--show-generated` | off | Print the generated test file |
| `--save-generated` | — | Save generated test to this path |

### `ordeal explore`

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
```

The `--workers` / `-w` flag runs exploration across multiple processes. Each worker gets a unique seed for independent state-space exploration. Results are aggregated: runs/steps are summed, edges are unioned for true unique count. Use `--workers $(nproc)` for full CPU utilization.

### `ordeal replay`

Reproduce a failure from a saved trace. The trace file contains the exact sequence of rules and fault toggles that triggered the failure, so replaying it re-executes the same steps.

Use for: triaging a CI failure, sharing a reproducible bug with a colleague, verifying that a fix actually resolves the issue.

```bash
ordeal replay .ordeal/traces/fail-run-42.json          # reproduce
ordeal replay --shrink trace.json                       # minimize
ordeal replay --shrink trace.json -o minimal.json      # save minimized
```

The `--shrink` flag runs delta-debugging to remove unnecessary steps from the trace. Use it when: the trace is too long to understand, or you want the minimal sequence of operations that reproduces the failure. The shrunk trace is often 5-10x shorter than the original.

## Workflows

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

When `--chaos` is active, ordeal prints a property report at the end of the test run. It shows every tracked property, its type, hit count, and pass/fail status:

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
