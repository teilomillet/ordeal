---
title: Close Reliability Evidence Gaps
description: Read the reliability map, deepen one safe gap, save continuity, and prioritize changed code.
---

# Close Reliability Evidence Gaps

## Start with the normal scan

```bash
ordeal scan .
```

The summary includes a reliability map count and one concrete next command.
Normal scan remains read-only with respect to project artifacts, but it imports
and executes target code.

```text
reliability map: 4 operations | PASS 1 | NOT EXERCISED 6 | FAIL 0 | blocked 1
next evidence: ordeal scan payments.gateway --target charge -n 100
```

## Run one safe follow-up automatically

Automatic deepening requires an explicit total time budget:

```bash
ordeal scan . --deepen --time-limit 60
```

Ordeal executes at most one `auto_runnable` experiment in a child process. The
initial scan observes its cooperative time limit; the child receives a hard
timeout for only the remaining budget. The child runs in its own process group,
and budget expiry terminates that process tree. Mutation, stateful workloads, and
differential checks remain planned commands when they require review instead of
being silently executed.

For a safe Python fault probe, the child records the exact operation, fault,
property, injection-hit count, and outcome. The parent merges that observation
and rebuilds the map. No injection hit means `NOT EXERCISED`; a clean return is
`PASS`; only a replayed uncaught exception is `FAIL`. Automatic probes currently
cover source-bound file-write failures and operations with exactly one fully
resolved `subprocess.run` command. Their injection branches prove that the fault
actually fired. Dynamic or multiple commands and other faults remain planned gaps.

## Prioritize a change

```bash
ordeal scan . --base-ref origin/main
```

Changed operations receive higher priority. When revision comparison is the
cheapest relevant check, the plan emits a complete `ordeal diff` command. CI can
set `ORDEAL_BASE_REF` instead of passing the flag. A missing or invalid revision
blocks changed-code prioritization instead of silently producing an empty set.

## Opt in to service faults

```bash
ordeal scan . --deepen --time-limit 180 --allow-service-faults
```

Compose is eligible only when `ordeal.toml` contains `[compose]`. The flag does
not make arbitrary mutating HTTP requests faultable; the Compose safety rules
still apply.

## Save continuity

```bash
ordeal scan . --save
```

The normalized plan is saved under `.ordeal/evidence-plans/`. A later scan reads
that file, reports new, removed, retained, and status-changed cells plus
source-hash changes, and carries prior input-source and configuration hints into
the current plan. Saving a plan with no replayable failure does not invent a
pytest regression.

## Read the real-project release evidence

The repository release gate measures scan plus deepening on three modern
bug/fixed reproductions bound to pinned HTTPie, PySnooper, and Tornado revisions.
It records precision, recall, time to witness, reliability-cell closure, and an
explicit out-of-scope reason when the scoped bug has no safely closable cell.
The locked baseline maps 8 cells and closes 0; that honest zero is a regression
gate, not a success claim, and any change requires explicit review.

## JSON consumers

```bash
ordeal scan . --json
```

Read `raw_details.reliability_map`. Candidate properties remain hypotheses.
Use `summary`, `next_experiment`, and `deepening` for the normal decision path;
use the normalized catalogs and cells for complete tooling.

See [Evidence Closure](../concepts/evidence-closure.md) for interpretation and
[Evidence Closure Schema](../reference/evidence-closure-schema.md) for fields.
