# ordeal — Chaos testing for Python

Your tests pass. Your code still breaks in production. The gap between "tests pass" and "code works" is the space of failures you never thought to test — timeouts during retries, NaN inside recovery paths, permission errors after partial writes. That space is enormous, and traditional testing doesn't cover it.

Ordeal closes this gap. You describe your system, declare what can go wrong, state what must stay true — and ordeal explores thousands of failure combinations automatically, with coverage guidance, until it either finds a violation or gives you confidence that your code handles adversity.

```python
from ordeal import ChaosTest, rule, always
from ordeal.faults import timing, numerical

class MyServiceChaos(ChaosTest):
    faults = [
        timing.timeout("myapp.api.call"),
        numerical.nan_injection("myapp.model.predict"),
    ]

    @rule()
    def call_service(self):
        result = self.service.process("input")
        always(result is not None, "process never returns None")

TestMyServiceChaos = MyServiceChaos.TestCase
```

```bash
pytest --chaos
```

When ordeal passes, it means something. Not "the tests pass" — but that the code was explored under adversity, with faults injected in combinations no human would write, and the invariants held.

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
