# Getting Started

From zero to your first chaos test in 5 minutes. By the end, you'll understand not just *how* to write a chaos test, but *why* each piece exists.

## Install

```bash
pip install ordeal
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add ordeal               # add to project
uv tool install ordeal       # install CLI globally
```

## The idea

Traditional tests check specific scenarios you thought of. A chaos test describes a *system* and lets the machine explore what can go wrong.

You define three things:

1. **Faults** — what can go wrong (timeout, NaN, crash, disk full). These are the bad things that happen in production but almost never appear in tests.
2. **Rules** — what your system does (process input, save data, read cache). These are the operations that users and services perform.
3. **Invariants** — what must always be true (no data corruption, no silent failures). These are the promises your system makes.

Ordeal takes these three ingredients and runs thousands of scenarios: different orderings of rules, different faults toggling on and off at different times. When something breaks, it tells you exactly which sequence caused the failure — shrunk to the minimum.

## Your first chaos test

Let's say you have a scoring service. It fetches data from an API and runs a model:

```python
# test_chaos.py
import math
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical


class ScoreServiceChaos(ChaosTest):
    """Chaos test for our scoring service.

    We declare two faults that happen in production:
    - The API sometimes times out
    - The model sometimes returns NaN (bad weights, edge-case input)
    """

    faults = [
        timing.timeout("myapp.api.fetch_data"),        # fault 1: API timeout
        numerical.nan_injection("myapp.model.predict"), # fault 2: model returns NaN
    ]

    def __init__(self):
        super().__init__()
        self.service = ScoreService()

    @rule()
    def score_user(self):
        """An operation the system performs. The nemesis may have
        activated faults before this runs — we don't know which ones."""
        try:
            result = self.service.score("user-123")
        except TimeoutError:
            return  # timeouts are expected — the system should handle them
        always(not math.isnan(result), "score is never NaN")
        always(0 <= result <= 1, "score in valid range")

    @invariant()
    def service_is_healthy(self):
        """Checked after every single step. Must always hold."""
        assert self.service.is_healthy()


# This one line makes pytest discover and run the chaos test
TestScoreServiceChaos = ScoreServiceChaos.TestCase
```

That's the complete test. Let's break down what each piece does.

### Faults: what can go wrong

```python
faults = [
    timing.timeout("myapp.api.fetch_data"),
    numerical.nan_injection("myapp.model.predict"),
]
```

Each fault targets a specific function by its dotted path. `timing.timeout("myapp.api.fetch_data")` means: "when this fault is active, calling `myapp.api.fetch_data` raises a `TimeoutError` instead of doing its real work."

Faults start inactive. The **nemesis** (explained below) toggles them on and off during the test.

### Rules: what the system does

```python
@rule()
def score_user(self):
    result = self.service.score("user-123")
    always(not math.isnan(result), "score is never NaN")
```

Rules are operations. They represent what users, services, or background jobs do to your system. Each run executes a random sequence of rules — the engine explores different orderings.

Inside rules, you place **assertions** — statements about what must be true. `always(condition, name)` means "this must be true every single time this line executes, across all runs." If it isn't, the test fails and the engine shrinks to the minimal example.

### Invariants: what must always hold

```python
@invariant()
def service_is_healthy(self):
    assert self.service.is_healthy()
```

Invariants are checked after *every single step* — after every rule, after every fault toggle. They express system-wide properties that must never be violated, regardless of what faults are active.

### The nemesis (you didn't write it — ordeal did)

There's a hidden player. Ordeal auto-injects a **nemesis rule** into your test. The nemesis is an adversary: at each step, it might toggle one of your faults on or off. You don't control when faults activate — the nemesis does, and Hypothesis explores the timing.

This is the key insight from [Jepsen](https://jepsen.io): a system needs an adversary during testing. Without one, you're only testing the happy path.

## Run it

```bash
pytest test_chaos.py -v                   # faults + invariants work
pytest test_chaos.py --chaos              # adds: always/sometimes tracking + buggify
pytest test_chaos.py --chaos --chaos-seed 42  # same as above, reproducible
```

## Understanding `--chaos` vs plain pytest

This matters. Get it wrong and your assertions silently do nothing.

**Without `--chaos`** (plain `pytest`):

- Faults toggle normally (the nemesis works)
- `@invariant()` methods run and `assert` statements work
- `always()` **raises on violation** — violations are never silent
- `unreachable()` **raises on violation** — violations are never silent
- `sometimes()` and `reachable()` don't track (no property report)
- `buggify()` always returns `False`

**With `--chaos`**:

- Everything above, plus:
- `sometimes()` and `reachable()` track hits — checked at session end
- `buggify()` returns `True` probabilistically (default 10%)
- A property report prints at the end showing all tracked properties

**The practical rule:**

| What you use in rules | Do you need `--chaos`? |
|---|---|
| `assert something` | No — works always |
| `always(condition, "name")` | No — raises on violation regardless |
| `unreachable("name")` | No — raises when reached regardless |
| `sometimes(condition, "name")` | **Yes** — not tracked without it |
| `reachable("name")` | **Yes** — not tracked without it |
| `buggify()` in production code | **Yes** — returns False without it |
| Faults (timeout, NaN, etc.) | No — nemesis toggles them regardless |
| `@invariant()` with `assert` | No — works always |

**The design principle:** violations are never silent. `always()` and `unreachable()` raise `AssertionError` whether or not `--chaos` is active. The `--chaos` flag adds the *tracking* layer (property report, `sometimes`/`reachable` deferred checks) and activates `buggify()`. But if something is wrong, you'll know immediately — no flag required.

## What happens under the hood

1. **Hypothesis generates a random sequence of rules.** It might run `score_user`, then `score_user` again, then the nemesis, then `score_user` — in any order, any number of times.

2. **The nemesis toggles faults.** At some point, it activates `nan_injection`. Now `myapp.model.predict` returns NaN instead of a real score. Later, it might deactivate it and activate `timeout` instead.

3. **After every step, all invariants are checked.** `service_is_healthy()` runs after every rule and every nemesis action. If the service becomes unhealthy at any point, the test fails.

4. **If an assertion fails, Hypothesis shrinks.** It doesn't just report the failure — it finds the *shortest* sequence of steps that reproduces it. Instead of "it failed after 47 steps," you get "it fails in 3 steps: activate NaN, call score_user, check invariant."

Example output:

```
FAILED test_chaos.py::TestScoreServiceChaos::runTest
  Falsifying example:
    state = ScoreServiceChaos()
    state._nemesis(data=...)      # activates nan_injection
    state.score_user()            # NaN propagates to output
    state.teardown()
```

Three steps. That's the minimal reproduction. Now you know exactly what to fix.

## Go deeper

You've written a chaos test. Here's where to go next, depending on what you want:

**Understand the concepts:**

- [Chaos Testing](concepts/chaos-testing.md) — how faults, nemesis, and swarm mode work together
- [Property Assertions](concepts/property-assertions.md) — always, sometimes, reachable, unreachable
- [Coverage Guidance](concepts/coverage-guidance.md) — how the explorer systematically finds bugs

**Use more features:**

- [Explorer](guides/explorer.md) — coverage-guided exploration with `ordeal explore`
- [Configuration](guides/configuration.md) — `ordeal.toml` for reproducible, shareable test runs
- [Auto Testing](guides/auto.md) — point ordeal at your module, get tests automatically

**Understand the philosophy:**

- [Philosophy](philosophy.md) — why ordeal exists and what it means for code quality
