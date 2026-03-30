# ordeal — Chaos testing for Python

ordeal is an automated chaos testing library for Python that combines fault injection, property-based assertions, coverage-guided exploration, and stateful testing in a single toolkit.

It brings ideas from [Antithesis](https://antithesis.com) (deterministic exploration), [FoundationDB](https://apple.github.io/foundationdb/testing.html) (BUGGIFY inline faults), [Jepsen](https://jepsen.io) (nemesis interleaving), [Hypothesis](https://hypothesis.works) (stateful property testing), [Jane Street QuickCheck](https://blog.janestreet.com/quickcheck-for-core/) (boundary-biased generation), and [Meta ACH](https://engineering.fb.com) (mutation validation) to the Python ecosystem.

## Quick example

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

## What ordeal does

| Capability | Inspired by | Module |
|---|---|---|
| Stateful chaos testing with nemesis | Jepsen + Hypothesis | [`ordeal.chaos`](api-reference.md) |
| Coverage-guided exploration | Antithesis | [`ordeal.explore`](explorer.md) |
| Property assertions (`always`, `sometimes`, `reachable`) | Antithesis | [`ordeal.assertions`](core-concepts.md) |
| Inline fault injection (BUGGIFY) | FoundationDB | [`ordeal.buggify`](core-concepts.md) |
| Boundary-biased property testing | Jane Street QuickCheck | [`ordeal.quickcheck`](core-concepts.md) |
| AST mutation testing | Meta ACH | [`ordeal.mutations`](mutations.md) |
| Deterministic simulation (Clock, FileSystem) | No-mock testing | [`ordeal.simulate`](simulate.md) |
| IO / numerical / timing faults | Chaos engineering | [`ordeal.faults`](api-reference.md) |

## Install

```bash
pip install ordeal
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add ordeal
```

## Learn

- **[Getting Started](getting-started.md)** — Write your first chaos test in 5 minutes
- **[Core Concepts](core-concepts.md)** — ChaosTest, faults, assertions, invariants, buggify, quickcheck

## Features

- **[Auto](auto.md)** — Zero-boilerplate: `scan_module`, `fuzz`, `chaos_for`
- **[Explorer](explorer.md)** — Coverage-guided exploration with AFL-style edge hashing and checkpointing
- **[Simulation](simulate.md)** — Deterministic Clock and FileSystem for no-mock testing
- **[Mutations](mutations.md)** — Validate that your chaos tests actually catch bugs
- **[Integrations](integrations.md)** — Atheris coverage-guided fuzzing, Schemathesis API testing

## Reference

- **[CLI](cli.md)** — `ordeal explore`, `ordeal replay`, pytest `--chaos` flag
- **[Configuration](configuration.md)** — `ordeal.toml` schema and options
- **[API Reference](api-reference.md)** — Complete public API
