---
title: Scan Finding Rules
description: How ordeal scan filters helper targets, chooses security probes, and promotes findings.
---

# Scan Finding Rules

!!! quote "In plain English"
    `ordeal scan` prefers real product callables over harness helpers, only mutates parameters that actually feed inferred sinks, and refuses to promote critical-sink crashes unless the proof bundle can replay them.

## Try it

```bash
ordeal scan mypkg --list-targets
ordeal scan mypkg --security-focus --save-artifacts
```

The first command shows how ordeal ranks the surface. The second shows which crashes become promoted findings and what lands in the proof bundle.

## 1. Fixtures and factories are support evidence first

- Pytest fixtures, `make_*`, `prime_*`, and setup/scenario helpers are still mined because they help build `[[objects]]` suggestions.
- Broad package-root scans and `--list-targets` now deprioritize those harness-only helpers so user-facing exports rise first.
- If a helper really is the thing you want to inspect, target it explicitly with `--target`.

## 2. Security probes only touch real sink-bearing parameters

- `--security-focus` no longer mutates every parameter that merely has a suggestive name.
- Deterministic probes only target parameters whose semantic bucket aligns with a sink that is backed by source evidence in the callable.
- A parameter named `config` is not enough by itself; the code must also look like it actually loads, parses, imports, writes, or attaches through that path.

## 3. `critical_sinks` is witness-aligned

- `impact.critical_sinks` and `contract_basis.critical_sinks` describe the sinks supported by the failing witness, not every sink inferred for the callable.
- The broader callable-level inference is still preserved under `impact.callable_sink_categories` and `contract_basis.callable_sink_categories`.
- This keeps proof bundles honest when one callable has several possible trust-boundary paths but only one is exercised by the failing input.

## 4. Critical sinks need replay-backed proof before promotion

- Normal `likely_bug` crashes still promote when their contract-fit, reachability, and realism scores clear the bar.
- If `critical_sinks` is non-empty, ordeal also requires a replayable proof bundle before treating the crash as a top finding.
- When replay does not confirm the witness, ordeal keeps the crash exploratory and writes the reason into `verdict.demotion_reason`.

## Where to look in saved artifacts

- `.ordeal/findings/<module>.proofs.json` keeps the proof bundle for each saved finding.
- `impact.critical_sinks` tells you which high-risk sink the witness actually reached.
- `impact.callable_sink_categories` tells you the wider sink surface the callable appears to expose.
- `verdict.promoted` and `verdict.demotion_reason` tell you whether the crash became a top finding or stayed exploratory.

