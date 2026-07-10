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
timeout for only the remaining budget. Mutation, stateful workloads, and
differential checks remain planned commands when they require review instead of
being silently executed.

For a safe Python fault probe, the child records the exact operation, fault,
property, injection-hit count, and outcome. The parent merges that observation
and rebuilds the map. No injection hit means `NOT EXERCISED`; a clean return is
`PASS`; only a replayed uncaught exception is `FAIL`. Automatic probes are
currently limited to source-bound file-write failures, whose injection branch
can prove that the fault actually fired. Other faults remain planned gaps.

## Prioritize a change

```bash
ordeal scan . --base-ref origin/main
```

Changed operations receive higher priority. When revision comparison is the
cheapest relevant check, the plan emits a complete `ordeal diff` command. CI can
set `ORDEAL_BASE_REF` instead of passing the flag.

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

## JSON consumers

```bash
ordeal scan . --json
```

Read `raw_details.reliability_map`. Candidate properties remain hypotheses.
Use `summary`, `next_experiment`, and `deepening` for the normal decision path;
use the normalized catalogs and cells for complete tooling.

See [Evidence Closure](../concepts/evidence-closure.md) for interpretation and
[Evidence Closure Schema](../reference/evidence-closure-schema.md) for fields.
