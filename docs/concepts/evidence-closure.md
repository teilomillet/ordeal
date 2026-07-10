---
title: Evidence Closure
description: How scan turns source seams, candidate properties, and measured results into an honest reliability plan.
---

# Evidence Closure

!!! quote "In plain English"
    A green test run says what passed. An evidence-closure map also says which
    important failure stories were never tried and what Ordeal can safely do next.

## The missing question

Code often contains clues about reliability obligations: an HTTP call inside a
retry loop, a transaction with rollback, a cache fallback, or a model loaded
from a checkpoint. Ordinary coverage does not say whether the relevant timeout,
restart, stale state, or shape drift was tested.

`ordeal scan` connects three kinds of information:

1. **Source seams** such as retry, fallback, recovery, cache, file, HTTP,
   subprocess, transaction, and model loading.
2. **Candidate properties** inferred from source, types, documentation,
   assertions, tests, and schemas.
3. **Measured evidence** from scan, stateful exploration, mutation, differential
   comparison, or an explicitly enabled Compose run.

The result is an operation × fault × property map.

## Read every cell literally

| Status | Meaning |
|---|---|
| `PASS` | The named operation/fault/property combination was observed and held |
| `NOT EXERCISED` | The combination was inferred or declared but not observed |
| `FAIL` | The combination was observed and violated |

A `blocking_reason` explains why a cell stayed unexercised. A strategy,
import, fixture, or harness construction problem is an Ordeal limitation; it is
never reported as a target crash.

## Hypotheses are not requirements

Source-mined properties always carry `epistemic_status = "hypothesis"` and
provenance. They are useful proposals, not business truth. A human-authored
contract or assertion remains the authority for what the software must do.

Safe Python fault probes add a narrower operational hypothesis: the operation
completes without an uncaught exception under one named fault. That cell changes
only when the injection boundary was actually reached. It does not silently
promote a broader idempotency, recovery, or data-integrity hypothesis.

## One entry point, several engines

The map chooses the cheapest next experiment without making a newcomer select
an internal engine. Depending on the gap, the planned command can use targeted
scan, mutation, exploration, revision diff, or Compose. The default automatic
path only runs safe targeted scans.

Service faults require both `--deepen` and `--allow-service-faults`, plus a
reviewed `[compose]` configuration. Python-level execution never claims control
over operating-system scheduling.

Continue with the [Evidence Closure Guide](../guides/evidence-closure.md) or use
the [exact schema](../reference/evidence-closure-schema.md).
