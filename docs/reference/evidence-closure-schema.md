---
title: Evidence Closure Schema
description: Exact normalized reliability map, continuity, blocked-result, and deepening fields.
---

# Evidence Closure Schema

The map schema is `ordeal.reliability-map/v1`. It appears at
`raw_details.reliability_map` and, after `scan --save`, under
`.ordeal/evidence-plans/`.

## Top-level fields

| Field | Meaning |
|---|---|
| `module` | Scanned package or module |
| `base_ref` | Optional Git comparison base |
| `service_faults_enabled` | Whether the user explicitly enabled service faults |
| `summary` | Operation, cell, PASS, NOT EXERCISED, FAIL, and blocked counts |
| `operations[]` | Source-bound callable records |
| `properties[]` | Property names; mined entries are `hypothesis` |
| `experiments[]` | Deduplicated executable commands and safety policy |
| `cells[]` | Normalized operation × fault × property evidence |
| `next_experiment` | Highest-priority safe automatic experiment, if one exists |
| `productive_hints` | Input-source and reviewed config hints safe to carry forward |
| `continuity` | Diff against the prior saved map |
| `deepening` | Budget and result of `--deepen`, when requested |
| `reliability_observations[]` | Exact fault-probe inputs, hit counts, and outcomes |

## Operation

`operations[]` includes `id`, `target`, `source`, `source_sha256`, `priority`,
`changed_since_base`, seam/profile names, property IDs, and provenance kinds.
`test_evidence` contains bounded source locations; it does not mean those tests
were executed by the scan. Provenance kinds include `source`, `test`,
`assertion`, `types`, `documentation`, and `schema`; all remain static evidence
until an experiment executes the property.

## Cell

| Field | Meaning |
|---|---|
| `id` | Stable hash of operation, seam, fault, and property |
| `operation_id` | Reference into `operations[]` |
| `property_id` | Reference into `properties[]` |
| `next_experiment_id` | Reference into `experiments[]` |
| `seam` / `fault` | Inferred production seam and proposed fault |
| `status` | `PASS`, `NOT EXERCISED`, or `FAIL` |
| `blocking_reason` | Why execution evidence could not be obtained |

Only runtime evidence can produce PASS or FAIL. Static analysis creates
NOT EXERCISED hypotheses.

Fault-probe observations use the property prefix
`operation completes without an uncaught exception under`. `injection.hits`
must be positive before that cell can become PASS or FAIL. FAIL additionally
requires the same uncaught outcome to replay; otherwise the cell remains NOT
EXERCISED with a blocker. An expected-precondition exception is not a clean
return and cannot become PASS. Automatic Python closure currently covers only
source-bound file-write faults and operations with exactly one fully resolved
`subprocess.run` timeout boundary. Dynamic or multiple commands remain NOT EXERCISED.

## Experiment safety

`safety` is `safe`, `review_required`, or `service_faults_opted_in`.
`auto_runnable` is true only for targeted scan or explicitly enabled Compose
work. Every `command` is complete—no placeholder selector is emitted.

## Continuity

`continuity` contains cell counts and bounded ID lists for new, removed,
retained, and status-changed cells. It also reports new, removed, and
source-changed operations by source hash. `truncated` means an ID list was
capped at 50; the count remains authoritative. `carried_forward_hints` is
present when a prior valid map was loaded; those seeds and config suggestions
are deduplicated into the current `productive_hints` catalog.

## Blocked scan evidence

Tool-side failures use `ordeal.scan-limitation/v1`, `status = "blocked"`, a
`limitation.kind`, and a reason. Its boundary explicitly says target behavior was not observed.
It is not a crash card and cannot produce a regression.

## Deepening

`deepening.status` is `completed`, `budget_exhausted`, `review_required`,
`no_safe_experiment`, or `error`. The record includes the budget, elapsed time,
engine, command, exit code when available, whether service faults executed, and
up to ten bounded child-finding summaries (`findings_truncated` marks overflow).
