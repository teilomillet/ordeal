---
description: >-
  Write your first Python chaos test in 5 minutes with ordeal. Fault
  injection, property assertions, automatic shrinking. From install to
  your first bug.
---

# Getting Started

From zero to your first chaos test in 5 minutes. By the end, you'll understand not just *how* to write a chaos test, but *why* each piece exists.

Throughout this page, **highlighted blocks** explain concepts in plain English — start with those if the code feels unfamiliar, or skip them if you just want the mechanics.

!!! quote "What is ordeal, and why should I care?"
    Your tests pass. Every single one. Then your code hits production and breaks — because a network call timed out, a database returned unexpected data, or two things happened in an order you never imagined.

    **Ordeal finds those failures before production does.** Instead of you writing every scenario by hand, ordeal generates thousands of them automatically — including ones where things go wrong. If your code breaks under any of them, ordeal shows you the simplest example that causes the failure.

    Think of it like a flight simulator for your code: it throws realistic failures at your system in controlled conditions, so you discover what breaks *before* real users do.

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

!!! quote "The shift in thinking"
    With regular tests, **you** decide what to test: "if I pass 5, I should get 10." You're the one imagining what might go wrong.

    With ordeal, **you describe your system** — what it does, what can break, and what must always be true — and the **machine** explores thousands of scenarios for you. It's like telling someone "here are the rules of the game and the ways it could cheat" and letting them play a thousand rounds to find every exploit.

    This means ordeal can find failures you never imagined, because it's not limited to your imagination.

You define three things:

1. **Faults** — what can go wrong (timeout, NaN, crash, disk full). These are the bad things that happen in production but almost never appear in tests.
2. **Rules** — what your system does (process input, save data, read cache). These are the operations that users and services perform.
3. **Invariants** — what must always be true (no data corruption, no silent failures). These are the promises your system makes.

Ordeal takes these three ingredients and runs thousands of scenarios: different orderings of rules, different faults toggling on and off at different times. When something breaks, it tells you exactly which sequence caused the failure — shrunk to the minimum.

## Your first chaos test

Let's say you have a scoring service. It fetches data from an API and runs a model:

!!! quote "What we're about to build"
    We're going to write a test that answers: **"Does our scoring service behave correctly even when things go wrong?"**

    The test will simulate two real-world failures — an API timeout and corrupted model output — and check that our service handles them without violating its promises. You don't need to write a test case for every failure combination. You describe the failures and the promises, and ordeal tries thousands of combinations for you.

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

!!! quote "Think of it this way"
    Faults are **controlled failures you inject into your code**. In production, your API might time out once a week. Your model might return garbage once a month. You can't wait for these to happen naturally — so you simulate them.

    Each fault says: "take this specific function and, when I tell you, make it misbehave in this specific way." The function itself is untouched — the fault wraps it temporarily during testing.

```python
faults = [
    timing.timeout("myapp.api.fetch_data"),
    numerical.nan_injection("myapp.model.predict"),
]
```

Each fault targets a specific function by its dotted path. `timing.timeout("myapp.api.fetch_data")` means: "when this fault is active, calling `myapp.api.fetch_data` raises a `TimeoutError` instead of doing its real work."

Faults start inactive. The **nemesis** (explained below) toggles them on and off during the test.

### Rules: what the system does

!!! quote "Think of it this way"
    Rules are the **normal operations** your system performs — the things users, services, or background jobs do every day. "Score a user," "save a record," "fetch a report."

    Inside each rule, you place **assertions**: promises about what should be true after the operation runs. `always(condition, "name")` is a promise that says "this must be true every time, no exceptions." When ordeal runs your test, it picks rules in random order and runs them many times. Some runs have faults active, others don't. If any promise is ever broken, ordeal catches it.

```python
@rule()
def score_user(self):
    result = self.service.score("user-123")
    always(not math.isnan(result), "score is never NaN")
```

Rules are operations. They represent what users, services, or background jobs do to your system. Each run executes a random sequence of rules — the engine explores different orderings.

Inside rules, you place **assertions** — statements about what must be true. `always(condition, name)` means "this must be true every single time this line executes, across all runs." If it isn't, the test fails and the engine shrinks to the minimal example.

### Invariants: what must always hold

!!! quote "Think of it this way"
    If rules are about "what happens during an operation," invariants are about "what must be true *between all operations*."

    An invariant is a fundamental promise your system makes: "no matter what just happened — fault, rule, nemesis action — this property holds." Think of it like a heartbeat monitor: it doesn't care what procedure is happening, it just checks that the patient's heart is still beating. If it stops, everything stops.

```python
@invariant()
def service_is_healthy(self):
    assert self.service.is_healthy()
```

Invariants are checked after *every single step* — after every rule, after every fault toggle. They express system-wide properties that must never be violated, regardless of what faults are active.

### The nemesis (you didn't write it — ordeal did)

!!! quote "Why an adversary?"
    In real life, you don't get to choose when things go wrong. The database doesn't crash when it's convenient. The network doesn't time out when you're ready for it.

    The nemesis captures this reality. It's an automatic opponent that decides *when* your faults activate, in *what combination*, and in *what order* relative to your normal operations. This is how ordeal finds the failures you'd never think to test — the ones that happen because fault A was active while operation B ran, which left the system in a state where operation C breaks.

There's a hidden player. Ordeal auto-injects a **nemesis rule** into your test. The nemesis is an adversary: at each step, it might toggle one of your faults on or off. You don't control when faults activate — the nemesis does, and Hypothesis explores the timing.

This is the key insight from [Jepsen](https://jepsen.io): a system needs an adversary during testing. Without one, you're only testing the happy path.

## Run it

```bash
pytest test_chaos.py -v                   # faults + invariants work
pytest test_chaos.py --chaos              # adds: always/sometimes tracking + buggify
pytest test_chaos.py --chaos --chaos-seed 42  # same as above, reproducible
```

!!! quote "What to expect when you run this"
    The first time you run a chaos test, it might feel different from regular tests. It runs for a few seconds (not milliseconds) because it's exploring many scenarios, not just one.

    **If the test passes**, ordeal tried hundreds of fault combinations and orderings and your system handled all of them correctly. That's a much stronger statement than "my 5 hand-written tests passed."

    **If the test fails**, ordeal shows you the *shortest sequence of events* that causes the failure. Not "it broke somewhere in 200 steps," but "these 3 specific things, in this order, break it." That's the failure you need to fix — and now you know exactly what it is.

## Understanding `--chaos` vs plain pytest

!!! quote "The simple version"
    If your test only uses `always()`, `unreachable()`, and `@invariant()`, you don't need `--chaos`. Violations raise immediately either way.

    You need `--chaos` for two extras: (1) tracking `sometimes()` and `reachable()` — assertions that check "this should happen *at least once* across all runs," and (2) activating `buggify()` — inline faults you can embed in your production code. If you're just starting out, you can ignore `--chaos` and add it later when you need these features.

This matters. Get it wrong and your assertions silently do nothing.

**Without `--chaos`** (plain `pytest`):

- Faults toggle normally (the nemesis works)
- `@invariant()` methods run and `assert` statements work
- `always()` **raises on violation** — violations are never silent
- `unreachable()` **raises on violation** — violations are never silent
- `sometimes()` and `reachable()` don't track (no deferred checks)
- `sometimes(..., warn=True)` prints status to stdout — visible without `--chaos`
- `buggify()` always returns `False`
- The property report prints if there are any tracked results

**With `--chaos`**:

- Everything above, plus:
- `sometimes()` and `reachable()` track hits — checked at session end
- `buggify()` returns `True` probabilistically (default 10%)

**The practical rule:**

| What you use in rules | Do you need `--chaos`? |
|---|---|
| `assert something` | No — works always |
| `always(condition, "name")` | No — raises on violation regardless |
| `unreachable("name")` | No — raises when reached regardless |
| `sometimes(condition, "name")` | **Yes** — not tracked without it |
| `sometimes(condition, "name", warn=True)` | No — prints to stdout without it |
| `reachable("name")` | **Yes** — not tracked without it |
| `buggify()` in production code | **Yes** — returns False without it |
| Faults (timeout, NaN, etc.) | No — nemesis toggles them regardless |
| `@invariant()` with `assert` | No — works always |
| Property report | No — prints whenever there are tracked results |

**The design principle:** violations are never silent. `always()` and `unreachable()` raise `AssertionError` whether or not `--chaos` is active. The `--chaos` flag adds the *tracking* layer (`sometimes`/`reachable` deferred checks) and activates `buggify()`. The property report prints whenever there are tracked results, regardless of flags. If something is wrong, you'll know immediately.

**Too loud?** If a known violation fires constantly and you need to focus on something else, pass `mute=True`:

```python
always(response.ok, "API healthy", mute=True)  # tracked in report, doesn't raise
```

The violation is still recorded and shows in the property report — it's tracked, not hidden. You see it, you just don't get interrupted by it. Remove `mute=True` when you're ready to fix it.

## What happens under the hood

!!! quote "The big picture"
    Ordeal's engine repeats a simple loop: **pick an action, run it, check everything.** The actions are your rules and the nemesis (which toggles faults). The checks are your invariants and assertions. It runs this loop across hundreds of different random sequences.

    When something breaks, it doesn't just stop — it **simplifies**. It removes steps one at a time to find the shortest sequence that still triggers the failure. This is called *shrinking*, and it's why ordeal gives you a clear, actionable failure instead of a wall of noise.

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

!!! quote "What you now understand"
    You've just learned the core of chaos testing. Here's what you can now explain:

    - **What it does**: ordeal automatically explores thousands of scenarios — including realistic failures — to find bugs your regular tests miss.
    - **How it works**: you describe what can go wrong (faults), what your system does (rules), and what must always be true (invariants). An adversary (the nemesis) controls when faults activate, in what combination and order.
    - **What you get**: when something breaks, ordeal gives you the shortest sequence of events that reproduces the failure — a clear path to the fix, not a wall of noise.
    - **Why it's valuable**: it tests combinations and orderings you'd never think of. Passing a chaos test provides stronger evidence of correctness than passing a fixed suite of hand-written scenarios, because it explores the space of failures rather than checking a few you imagined.

!!! quote "Where you go from here depends on what you need"
    Need results fast? `ordeal mine mymodule` finds bugs in 30 seconds with zero test code.

    Building a test suite? The [Writing Tests](guides/writing-tests.md) guide has patterns you can copy and adapt for your own services.

    Evaluating for your team? Run `ordeal audit` on your existing modules — it shows you exactly what ordeal adds on top of your current tests.

## Go deeper

You've written a chaos test. Here's where to go next, depending on what you want:

**Discover what's available:**

```python
from ordeal import catalog
c = catalog()  # returns all faults, invariants, assertions, strategies, integrations
c["faults"]    # all fault types with names, signatures, and docs
```

`catalog()` gives you runtime discovery of everything ordeal offers — every fault, every invariant, every strategy, with signatures and documentation. AI assistants can use this to find the right tool for any situation.

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
