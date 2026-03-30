# Core Concepts

Ordeal's design is built on a few key ideas. Each concept has its own deep-dive page.

## Understand

- **[Chaos Testing](concepts/chaos-testing.md)** — What chaos testing is, how ChaosTest works, the nemesis, swarm mode. The foundation.

- **[Property Assertions](concepts/property-assertions.md)** — `always`, `sometimes`, `reachable`, `unreachable`. The Antithesis assertion model: state what must be true, let the machine find violations.

- **[Fault Injection](concepts/fault-injection.md)** — External faults (PatchFault) and inline faults (buggify). The FoundationDB model for Python.

- **[Coverage Guidance](concepts/coverage-guidance.md)** — How the Explorer uses edge hashing, checkpoints, and energy scheduling to find bugs that random testing misses.

- **[Shrinking](concepts/shrinking.md)** — How ordeal minimizes failures: delta debugging, step elimination, fault simplification. From a 50-step trace to 3.

## Quick reference

| Concept | One-liner | Origin |
|---|---|---|
| ChaosTest | Stateful test with nemesis + swarm | Jepsen + Hypothesis |
| Assertions | Temporal properties across runs | Antithesis |
| Faults | External fault injection via PatchFault | Chaos engineering |
| Buggify | Inline fault gates — no-op in production | FoundationDB |
| Explorer | Coverage-guided exploration with checkpoints | Antithesis + AFL |
| Shrinking | Minimize failing traces to minimal reproduction | Delta debugging |
| QuickCheck | Boundary-biased property testing | Jane Street |
| Invariants | Composable checks: `finite & bounded(0, 1)` | — |
