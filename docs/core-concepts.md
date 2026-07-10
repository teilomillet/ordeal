---
description: >-
  Core concepts behind ordeal: ChaosTest, property assertions, fault
  injection, buggify, coverage-guided exploration, and shrinking. Quick
  reference for Python chaos testing.
---

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
    - **Want to know which fault behavior was really tested?** → [Reliability Coverage](concepts/reliability-coverage.md)
    - **Want to know whether your tests would catch wrong behavior?** → [Meaningful Tests](concepts/test-meaningfulness.md)
    - **Want to understand a failure ordeal found?** → [Shrinking](concepts/shrinking.md)
    - **Want to inject specific faults?** → [Fault Injection](concepts/fault-injection.md)
    - **Want to compare a function before and after a rewrite?** → [Differential Testing](concepts/differential-testing.md)
    - **Want to compare a whole workflow before and after a refactor?** → [System Differential Testing](concepts/system-differential.md)
    - **Want to replace a module without copying its old bugs?** → [Safe Module Migrations](concepts/safe-migrations.md)

- **[Chaos Testing](concepts/chaos-testing.md)** — What chaos testing is, how ChaosTest works, the nemesis, swarm mode. The foundation.

- **[Property Assertions](concepts/property-assertions.md)** — `always`, `sometimes`, `reachable`, `unreachable`. The Antithesis assertion model: state what must be true, let the machine find violations.

- **[Fault Injection](concepts/fault-injection.md)** — External faults (PatchFault) and inline faults (buggify). The FoundationDB model for Python.

- **[Coverage Guidance](concepts/coverage-guidance.md)** — How the Explorer uses edge hashing, checkpoints, and energy scheduling to find bugs that random testing misses.

- **[Reliability Coverage](concepts/reliability-coverage.md)** — Operation × fault × property evidence. Distinguishes tested-and-passed behavior from expected behavior that never ran.

- **[Meaningful Tests](concepts/test-meaningfulness.md)** — Why coverage and passing tests are not enough; mutation score, attribution, property strength, and scoped protection verdicts.

- **[Shrinking](concepts/shrinking.md)** — How ordeal minimizes failures: delta debugging, step elimination, fault simplification. From a 50-step trace to 3.

- **[Differential Testing](concepts/differential-testing.md)** — Why both versions need isolated inputs, what the outcome envelope contains, and how to read four honest statuses.

- **[System Differential Testing](concepts/system-differential.md)** — Replay the same operations and fault plan against two versions, then compare interface, outcomes, state, effects, recovery, and speed.

- **[Safe Module Migrations](concepts/safe-migrations.md)** — Why matching old behavior is not enough, and how invariants, mutation testing, and a candidate-only scan protect the replacement. Then run the [complete workflow](guides/migration-workflow.md).

!!! quote "In plain English"
    The table below is a cheat sheet. The "Origin" column shows where each idea comes from — industry-proven approaches from teams that build systems where bugs cost millions. Ordeal packages these ideas so you get the benefits without needing to know the history.

    You do not need to know those tools. Just use ordeal and the lessons are already built in.

## Quick reference

| Concept | One-liner | Origin |
|---|---|---|
| ChaosTest | Stateful test with nemesis + swarm | Jepsen + Hypothesis |
| Assertions | Temporal properties across runs | Antithesis |
| Reliability coverage | Operation × fault × property evidence | Ordeal |
| Faults | External fault injection via PatchFault | Chaos engineering |
| Buggify | Inline fault gates — no-op in production | FoundationDB |
| Explorer | Coverage-guided exploration with checkpoints | Antithesis + AFL |
| Shrinking | Minimize failing traces to minimal reproduction | Delta debugging |
| QuickCheck | Boundary-biased property testing | Jane Street |
| Test protection | Coverage + mutation survival + property evidence | Meta ACH + specification mining |
| Differential testing | Isolated old-versus-new outcome envelopes | Ordeal + Hypothesis |
| System differential | One shared operation + fault story across two versions | Ordeal |
| Safe migration | Parity plus explicit correctness and test-strength checks | Ordeal |
| Invariants | Composable checks: `finite & bounded(0, 1)` | — |
