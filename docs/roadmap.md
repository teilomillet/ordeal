---
title: Functional Coverage Roadmap
description: >-
  Near-term Ordeal priorities for functionality coverage, seam coverage, and
  native-boundary findings.
---

# Functional Coverage Roadmap

!!! quote "Direction"
    Ordeal should get better at covering behavior, not drift into generic offensive security tooling. The target is simple: cover more real service behavior, explain what was and was not exercised, and turn failures into small readable regressions.

## What exists now

- `ChaosTest` already has shrinkable fault-subset swarm mode in `ordeal/chaos.py`.
- The Explorer already has joint rule+fault swarm, coverage-directed configs, and energy updates in `ordeal/explore.py`.
- Native boundary testing exists at the subprocess layer: `subprocess_timeout()`, `subprocess_delay()`, and `corrupt_stdout()` in `ordeal/faults/io.py`.
- The gap is not "no swarm" or "no boundary testing." The gap is proving efficacy, exposing coverage, and turning child-process failures into first-class findings.

## P1: Next

### 1. Swarm observability and efficacy

- Outcome: know whether swarm improved coverage or findings on a target, not just that a subset was selected.
- Modules: `ordeal/explore.py`, `ordeal/chaos.py`, `ordeal/cli.py`, `tests/test_explore.py`.
- Ship when:
  - exploration results report the top swarm configs, dead configs, and edges/failures per config
  - the CLI can print a compact swarm summary
  - tests include a swarm-vs-no-swarm ablation on a benchmark target

### 2. Behavior coverage reporting

- Outcome: report covered behaviors, not only lines and raw edges.
- Modules: `ordeal/explore.py`, `ordeal/assertions.py`, `ordeal/state.py`, `ordeal/cli.py`.
- Ship when:
  - results include a `rule x fault x property` coverage view
  - reports call out unexercised retry, fallback, and recovery paths when detectable
  - traces show which properties were under stress when a failure happened

### 3. Native-boundary crash findings

- Outcome: treat "the worker died" as a first-class finding with a readable cause.
- Modules: `ordeal/faults/io.py`, `ordeal/supervisor.py`, `ordeal/trace.py`, `ordeal/cli.py`.
- Ship when:
  - Ordeal records child exit mode: nonzero code, signal death, timeout, truncated output
  - failures shrink to the smallest request or operation sequence that still kills the child
  - a crash in a model worker or helper process does not crash the main exploration run

### 4. Pairwise and t-wise swarm coverage

- Outcome: cover important fault combinations early instead of relying only on coin-flip subsets.
- Modules: `ordeal/explore.py`, `ordeal/chaos.py`, `ordeal/config.py`.
- Ship when:
  - swarm can target pairwise coverage of fault sets
  - configs can be budgeted by time or run count
  - results report uncovered fault pairs

## P2: After that

### 5. Long-lived worker harness

- Outcome: test persistent model workers, not only one-shot subprocess calls.
- Modules: `ordeal/supervisor.py`, `ordeal/faults/io.py`, `ordeal/integrations/http.py`.
- Ship when:
  - Ordeal can start a child once, send multiple requests, inject failures between requests, and restart on crash
  - traces preserve session state clearly enough to debug

### 6. ML and data seam fault packs

- Outcome: make the common ML failure modes one import away.
- Modules: `ordeal/faults/numerical.py`, `ordeal/faults/io.py`, `ordeal/integrations/http.py`, `ordeal/integrations/openapi.py`.
- Candidate packs:
  - shape drift
  - dtype drift
  - NaN and Inf bursts
  - partial batch results
  - stale model artifact or version skew
  - corrupt weights or feature order drift

### 7. Auto seam discovery

- Outcome: `scan` and `init` should detect likely service seams and propose relevant fault packs.
- Modules: `ordeal/auto.py`, `ordeal/cli.py`, `ordeal/state.py`.
- Ship when:
  - subprocess, HTTP, cache, file, and model-load seams are detected automatically
  - generated starter configs include likely faults and starter invariants

## P3: Later

- Coverage-guided swarm tuning that adapts pairwise budgets from observed dead zones
- Differential behavior coverage across service versions, model versions, or fallback implementations
- Corpus sharing for productive swarm configs and native-boundary traces across CI runs

## Non-goals

- Ordeal should not try to become a kernel, browser, or heap-exploitation framework.
- It does not need to replace ASAN, UBSan, or low-level native fuzzers.
- Its role is to catch service-level evidence: "this sequence of realistic operations kills or corrupts the worker," then shrink that to a readable regression.

## First milestone

Ship P1 in this order:

1. Swarm observability and CLI reporting
2. Native-boundary crash findings
3. Behavior coverage reporting
4. Pairwise swarm coverage

That sequence gives Ordeal better proof, better debugging value, and better seam coverage before it grows the surface area further.
