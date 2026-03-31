---
title: Chaos Testing for Python
description: >-
  Ordeal: automated chaos testing for Python. Fault injection, property
  assertions, mutation testing, and coverage-guided exploration. Find the
  bugs your tests miss.
---

# ordeal — Chaos testing for Python

Your tests pass. Your code still breaks in production. Ordeal finds what you missed.

!!! quote "Why ordeal"
    Your code works until it doesn't. Ordeal finds the failures you didn't think to test — the crash when two things happen at once, the silent wrong answer on a weird input, the timeout that only happens under load. It tries thousands of combinations no human would write by hand, and when it finds a problem, it hands you the exact steps to reproduce it. One command, real bugs, no test code required.

## What ordeal does

You give it your Python code. It gives you back:

- **What your functions actually do** — not what you think they do, what they *provably* do across hundreds of random inputs
- **What your tests miss** — gaps in coverage, mutations your tests don't catch, edge cases you haven't considered
- **Exactly what to fix** — line numbers, specific inputs, concrete suggestions

No test code to write. No configuration. Just point and run.

## Try it right now

Open a terminal and paste this ([uvx](https://docs.astral.sh/uv/guides/tools/) runs Python tools without installing them):

```bash
uvx ordeal mine ordeal.demo
```

This analyzes ordeal's built-in demo module. You'll see output like:

```
mine(score): 500 examples
  ALWAYS  output in [0, 1] (500/500)         ← score() always returns a value between 0 and 1
  ALWAYS  monotonically non-decreasing        ← bigger input = bigger output, always

mine(normalize): 500 examples
  ALWAYS  len(output) == len(xs) (500/500)    ← output is always the same length as input
     97%  idempotent (29/30)                  ← normalizing twice SHOULD give the same result
                                                 ...but ordeal found 1 case where it doesn't
```

Ordeal called each function hundreds of times with random inputs and told you what's always true — and what isn't. That `97% idempotent` is a real finding: there's an edge case where `normalize(normalize(x))` gives a different result than `normalize(x)`.

## Point it at your code

If your project has a file like `myapp/scoring.py`, the module path is `myapp.scoring` — the file path with slashes replaced by dots, without the `.py`:

```bash
uvx ordeal mine myapp.scoring       # what do my functions actually do?
uvx ordeal audit myapp.scoring      # what are my tests missing?
```

`audit` goes further — it generates tests for you, measures coverage, and mutation-tests the result:

```
myapp.scoring
  migrated:  12 tests |   130 lines | 96% coverage [verified]
  mutation: 14/18 (78%)                   ← ordeal flipped operators in your code;
                                             4 changes went undetected by your tests
  suggest:
    - L42 in compute(): test when x < 0
    - L67 in normalize(): test that ValueError is raised
```

Those `suggest` lines are real. Line 42 of `compute()` behaves differently with negative inputs, and your tests never check that.

## Let your AI assistant do it

You don't need to learn ordeal's API. Open Claude Code, Cursor, Copilot, or any AI coding assistant and paste:

> "Run `uv tool install ordeal` to install ordeal. Then run `ordeal mine` on each module in my project and `ordeal audit` on the ones with existing tests. Read the output, explain what it found, and fix the issues it suggests."

Or without installing anything:

> "Run `uvx ordeal mine` on my main modules. Show me the output and explain what the findings mean."

ordeal ships with an [AGENTS.md](https://github.com/teilomillet/ordeal/blob/main/AGENTS.md) — your AI assistant reads it automatically and knows every command, every option, and how to interpret every result.

## Install

When you're ready to make ordeal part of your workflow:

```bash
pip install ordeal           # or: uv tool install ordeal
```

Then `ordeal mine`, `ordeal audit`, and `ordeal explore` are available directly from your terminal.

## Find what you need

Every goal maps to a starting point — a command to run, a module to import, and a page to read. Nothing is hidden.

| I want to... | Start here | Learn more |
|---|---|---|
| Find bugs without writing tests | `ordeal mine mymodule` | [Auto Testing](guides/auto.md) |
| Check if my tests are good enough | `ordeal audit mymodule` | [Mutations](guides/mutations.md) |
| Write a chaos test | `from ordeal import ChaosTest` | [Getting Started](getting-started.md) |
| Inject specific failures (timeout, NaN, ...) | `from ordeal.faults import timing` | [Fault Injection](concepts/fault-injection.md) |
| Explore all failure combinations | `ordeal explore` | [Explorer](guides/explorer.md) |
| Reproduce and shrink a failure | `ordeal replay trace.json` | [Shrinking](concepts/shrinking.md) |
| Add fail-safe gates to production code | `from ordeal.buggify import buggify` | [Fault Injection](concepts/fault-injection.md) |
| Make assertions across all runs | `from ordeal import always, sometimes` | [Assertions](concepts/property-assertions.md) |
| Control time / filesystem in tests | `from ordeal.simulate import Clock` | [Simulation](guides/simulate.md) |
| Compare two implementations | `ordeal mine-pair mod.fn1 mod.fn2` | [Auto Testing](guides/auto.md) |
| Test API endpoints for faults | `from ordeal.integrations.openapi import chaos_api_test` | [Integrations](guides/integrations.md) |
| Extend ordeal with a new fault | Follow the pattern in `ordeal/faults/*.py` | [Fault Injection](concepts/fault-injection.md) |
| Configure reproducible runs | Create `ordeal.toml` | [Configuration](guides/configuration.md) |
| Discover all available faults, assertions, strategies | `from ordeal import catalog; catalog()` | [API Reference](reference/api.md) |

!!! quote "Pick your starting point"
    Every path leads somewhere useful. You can't pick wrong.

    - **"I just want to see what ordeal does"** → Run `uvx ordeal mine ordeal.demo` in your terminal, then read [Getting Started](getting-started.md)
    - **"I have code and want to find bugs"** → Run `ordeal mine mymodule` — see [Auto Testing](guides/auto.md)
    - **"I want to write chaos tests for my service"** → Start with [Getting Started](getting-started.md), then [Writing Tests](guides/writing-tests.md)
    - **"I want to understand the ideas behind ordeal"** → Read [Philosophy](philosophy.md), then the [Concepts](core-concepts.md)
    - **"I need to check if my tests are any good"** → Run `ordeal audit` — see [Mutations](guides/mutations.md)
    - **"I want to run ordeal in CI"** → See the [Explorer guide](guides/explorer.md) and [Configuration](guides/configuration.md)
    - **"I want to explore the source code"** → See the [Architecture section in the README](https://github.com/teilomillet/ordeal#architecture--code-map) for a full code map

## Start here

<div class="grid cards" markdown>

-   **[Philosophy](philosophy.md)**

    Why ordeal exists. What problem it solves. Why it matters for the future of code quality.

-   **[Getting Started](getting-started.md)**

    Write your first chaos test in 5 minutes. From install to your first failure.

</div>

## Understand

<div class="grid cards" markdown>

-   **[Chaos Testing](concepts/chaos-testing.md)**

    What is chaos testing? Faults, nemesis, swarm mode — explained from the ground up.

-   **[Coverage Guidance](concepts/coverage-guidance.md)**

    How the explorer finds bugs: edge hashing, checkpoints, energy scheduling.

-   **[Property Assertions](concepts/property-assertions.md)**

    always, sometimes, reachable, unreachable — the Antithesis assertion model.

-   **[Fault Injection](concepts/fault-injection.md)**

    External faults, inline buggify, the FoundationDB model — and when to use each.

-   **[Shrinking](concepts/shrinking.md)**

    How ordeal minimizes failures: delta debugging, step elimination, fault simplification.

</div>

## Use

<div class="grid cards" markdown>

-   **[Explorer](guides/explorer.md)** — Run and configure coverage-guided exploration
-   **[Writing Tests](guides/writing-tests.md)** — Patterns for effective chaos tests
-   **[Auto Testing](guides/auto.md)** — Zero-boilerplate: scan_module, fuzz, mine, diff, chaos_for
-   **[Simulation](guides/simulate.md)** — Deterministic Clock and FileSystem
-   **[Mutations](guides/mutations.md)** — Validate that your tests catch real bugs
-   **[Integrations](guides/integrations.md)** — Atheris fuzzing, Schemathesis API testing

</div>

## Reference

<div class="grid cards" markdown>

-   **[CLI](guides/cli.md)** — ordeal explore, ordeal replay, pytest --chaos
-   **[Configuration](guides/configuration.md)** — ordeal.toml schema and tuning
-   **[API Reference](reference/api.md)** — Every function, every parameter, every type
-   **[Troubleshooting](troubleshooting.md)** — Common issues and how to fix them

</div>

## What ordeal brings together

| Capability | Idea | Origin |
|---|---|---|
| Stateful chaos testing | Nemesis toggles faults while Hypothesis explores interleavings | [Jepsen](https://jepsen.io) + [Hypothesis](https://hypothesis.works) |
| Coverage-guided exploration | Checkpoint interesting states, branch from productive ones | [Antithesis](https://antithesis.com) |
| Property assertions | `always`, `sometimes`, `reachable`, `unreachable` | [Antithesis](https://antithesis.com/docs/properties_assertions/) |
| Inline fault injection | `buggify()` — no-op in production, fault in testing | [FoundationDB](https://apple.github.io/foundationdb/testing.html) |
| Boundary-biased generation | Test at 0, -1, empty, max — where bugs cluster | [Jane Street](https://blog.janestreet.com/quickcheck-for-core/) |
| Mutation testing | Verify tests catch real code changes | [Meta ACH](https://engineering.fb.com) |
| Differential testing | Compare two implementations on random inputs | Regression testing |
| Property mining | Discover invariants from execution traces | Specification mining |
| Metamorphic testing | Check output *relationships* across transformed inputs | [Metamorphic relations](https://en.wikipedia.org/wiki/Metamorphic_testing) |
| Network faults | HTTP errors, rate limiting, DNS failure, connection reset | Real-world API failures |
| Concurrency faults | Lock contention, thread boundaries, stale state | Thread-safety testing |

## Install

```bash
pip install ordeal           # core
pip install ordeal[all]      # everything
uv tool install ordeal       # CLI tool
```
