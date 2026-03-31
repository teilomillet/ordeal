# Core Concepts

!!! quote "How it all fits together"
    The flow is simple: **you describe your system** → **ordeal explores what can go wrong** → **when something breaks, it shows you the simplest example**.

    That's it. You tell ordeal how your code works, it tries thousands of ways to break it, and when it finds a problem, it strips away everything that doesn't matter so you can see exactly what went wrong.

    Each concept below is one piece of this flow. You don't need to learn them all at once — start with what you need.

Ordeal's design is built on a few key ideas. Each concept has its own deep-dive page.

## Understand

!!! quote "What do you want to do?"
    - **Want to test your service for failures?** → [Chaos Testing](concepts/chaos-testing.md)
    - **Want to know what properties your code has?** → [Property Assertions](concepts/property-assertions.md)
    - **Want ordeal to find bugs automatically?** → [Coverage Guidance](concepts/coverage-guidance.md)
    - **Want to understand a failure ordeal found?** → [Shrinking](concepts/shrinking.md)
    - **Want to inject specific faults?** → [Fault Injection](concepts/fault-injection.md)

- **[Chaos Testing](concepts/chaos-testing.md)** — What chaos testing is, how ChaosTest works, the nemesis, swarm mode. The foundation.

- **[Property Assertions](concepts/property-assertions.md)** — `always`, `sometimes`, `reachable`, `unreachable`. The Antithesis assertion model: state what must be true, let the machine find violations.

- **[Fault Injection](concepts/fault-injection.md)** — External faults (PatchFault) and inline faults (buggify). The FoundationDB model for Python.

- **[Coverage Guidance](concepts/coverage-guidance.md)** — How the Explorer uses edge hashing, checkpoints, and energy scheduling to find bugs that random testing misses.

- **[Shrinking](concepts/shrinking.md)** — How ordeal minimizes failures: delta debugging, step elimination, fault simplification. From a 50-step trace to 3.

!!! quote "In plain English"
    The table below is a cheat sheet. The "Origin" column shows where each idea comes from — industry-proven approaches from teams that build systems where bugs cost millions. Ordeal packages these ideas so you get the benefits without needing to know the history.

    You do not need to know those tools. Just use ordeal and the lessons are already built in.

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
