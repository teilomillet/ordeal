---
title: Product Direction and Roadmap
description: >-
  Where Ordeal is going, what already works, and what its evidence means.
---

# Product Direction and Roadmap

## The one-minute version

Most tests check situations a developer thought to write down. Production bugs often hide between them: a timeout during a retry, a worker restart after a write, or a damaged response that reaches a recovery path.

Ordeal explores those combinations automatically. When something breaks, the goal is not a large log file.
The goal is a small, readable sequence that shows what happened and can become a permanent regression test.

### A concrete example

Imagine an order service whose normal test passes. In production, confirmation times out, the client retries, and the card is charged twice.

Ordeal can:

1. run the order operation while injecting the timeout
2. try the retry and recovery paths in different orders
3. detect that the “charge once” rule was broken
4. reduce the failure to the shortest sequence that still reproduces it
5. save that sequence so CI can guard the fix

## What you can use today

| Question | What Ordeal does |
| --- | --- |
| What unexpected input breaks this function? | `scan` explores inputs and saves replay evidence for supported findings. |
| Which failure combinations break this workflow? | `explore` varies operations, faults, state, and inputs with coverage guidance. |
| Did we actually test the retry or recovery rule? | Behavior coverage records operation × fault × property observations. |
| What killed or stalled a child process? | Native-boundary findings classify timeouts, signals, and nonzero exits. |
| Does a real service recover after a worker dies? | The Compose runner keeps services alive, injects faults, and replays traces. |
| Did a refactor change behavior? | [System differential runs](concepts/system-differential.md) compare outcomes, state, side effects, recovery, and budgets. |
| How do we stop this exact bug returning? | Saved findings can become durable pytest regressions and CI bindings. |

## How to read the evidence

Ordeal is deliberately precise about what a result proves:

- **Observed and held** means the property held in the scenarios that ran.
- **Not reached** means the run did not exercise that path. It does not mean the
  path is impossible.
- **Supported finding** means the saved witness matched the reported failure
  within its replay boundary. It is not a certificate for the whole program.
- **Attempted N / reproduced M** reports real-service replay honestly when
  timing or external systems prevent deterministic reproduction.

This distinction is central to the product: show useful evidence without turning a bounded experiment into a universal claim.

## What is already shipped

### Coverage that follows behavior

The Explorer records which rules ran, which faults were active, and which properties were observed.
Its summaries surface productive swarm configurations, stressed properties, uncovered fault pairs, and paths that were not reached.

### Real failure boundaries

Subprocess timeouts, signal deaths, and nonzero exits become structured
findings with the command and bounded output excerpts. Compose exploration
extends this to long-lived services and records exact actions plus repeated
replay counts.

### Combination and version exploration

Unified swarm varies rules and faults together, directs runs toward missing
fault pairs, and keeps the full configuration in the schedule. Differential
exploration applies one interleaved operation-and-fault sequence to two fresh
system versions and minimizes the first mismatch.

### Implementation evidence

- `ordeal/reliability.py` deepens exact file and timeout cells; the release gate measures pinned public pairs.
- `ordeal/explore.py` exposes swarm statistics, behavior coverage, property stress, native-boundary findings, and pairwise coverage.
- `tests/test_explore_telemetry.py` checks the structured telemetry and trace evidence.
- `ordeal/compose.py` and `tests/test_compose.py` cover service lifecycle, fault windows, recovery, trace replay, and evidence promotion.
- `ordeal/diff.py`, `ordeal/system_diff.py`, and `tests/test_system_diff.py` cover bounded comparisons between system versions.

## What comes next

### P1: strengthen the proof

1. **Measure swarm efficacy.** Run repeatable swarm-versus-no-swarm studies
   across sequence, accumulation, service, and process-boundary bugs.
2. **Deepen evidence closure.** The shipped reliability map connects retry,
   fallback, recovery, production I/O, and ML/data seams to observed or missing
   behavior cells; expand beyond exact file writes and resolved subprocess
   timeouts only when injection-hit attribution remains provable.
3. **Complete native-boundary evidence.** Distinguish truncated output and
   preserve minimized replay evidence across more process adapters.
4. **Go beyond pairs.** Make selected higher-order fault combinations
   configurable, budgeted, and visible in reports.

### P2: cover common production seams

- ready-made ML and data faults for shape drift, dtype drift, NaN/Inf bursts,
  partial batches, stale artifacts, and feature-order changes
- source-backed discovery of subprocess, HTTP, cache, file, and model-loading
  seams, with relevant starter faults and properties

### Later

- adapt combination budgets from observed coverage dead zones
- share productive swarm configurations and native-boundary traces across CI
  runs without overstating their portability

## What Ordeal is not

- It is not a proof that software is bug-free.
- It does not replace ordinary unit, integration, or security tests.
- It does not replace ASAN, UBSan, or low-level native fuzzers.
- Python-level determinism does not control an operating system or virtual
  machine; real-service timing remains a measured replay boundary.

## Small glossary

| Term | Plain-English meaning |
| --- | --- |
| Rule | An action the system can take, such as “create order.” |
| Fault | A realistic failure, such as a timeout or worker restart. |
| Property | Something that must remain true, such as “charge at most once.” |
| Trace | The recorded sequence of actions and faults. |
| Shrinking | Removing unnecessary steps until only the smallest failure remains. |
| Swarm | Running focused subsets of actions and faults to explore combinations. |
| Pairwise coverage | Evidence that every selected pair of faults ran together. |
