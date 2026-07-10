---
title: Revision Diff Schema
description: Exact JSON fields, statuses, evidence bindings, and artifact paths for ordeal diff.
---

# Revision diff schema

`ordeal diff --json` and the saved `.json` file use `schema_version = 1`.
Missing checks stay missing; consumers must not infer success from an absent
field.

## Top-level result

| Field | Meaning |
|---|---|
| `schema_version` | Result schema version, currently `1` |
| `tool` | Literal `ordeal diff` |
| `mode` | `function` or `system` when a sequence file drives a factory |
| `target` | Requested dotted callable or module |
| `status` | `divergent`, `no_divergence_observed`, or `inconclusive` |
| `claim` | Smallest statement justified by the measured run |
| `isolated` | Whether base and candidate worktree paths differed |
| `base`, `candidate` | Ref, resolved commit, worker PID, and temporary worktree |
| `settings` | `max_examples`, seed, tolerances, and replay attempts |
| `totals` | Function, example, mismatch, and supported-mismatch counts |
| `added_functions` | Candidate-only public functions |
| `removed_functions` | Base-only public functions |
| `candidate_resolution_error` | Candidate import/target failure, otherwise `null` |
| `functions` | Per-function comparison rows |
| `artifacts` | Embedded `ordeal.divergence-evidence/v1` records |
| `system_sequence` | Supplied operation/fault objects; empty in function mode |

When `--save-artifacts` is used with `--json`, stdout also includes
`saved_artifacts.json` and `saved_artifacts.markdown` paths. The saved result
keeps the embedded `artifacts` list and adds `generated_at` plus
`commands.rerun`.

## Revision runtime

`base` and `candidate` contain:

| Field | Meaning |
|---|---|
| `ref` | User-facing ref such as `origin/main` or `HEAD` |
| `commit` | Fully resolved commit SHA |
| `pid` | Worker process ID |
| `worktree` | Detached temporary worktree used for execution |

The path is evidence of isolation, not a durable artifact path. It is normally
gone by the time the command returns.

## Function rows

| Field | Meaning |
|---|---|
| `name` | Function name inside the selected target |
| `status` | Function-scoped bounded status |
| `base_signature`, `candidate_signature` | Inspected signatures |
| `signature_changed` | Whether the signatures differ |
| `total` | Generated base cases replayed in the candidate |
| `mismatch_count` | All observed outcome differences |
| `supported_mismatch_count` | Canonical promoted witnesses with complete bindings and full replay; at most one per function |
| `mismatches` | Zero or one canonical runtime witness with embedded evidence |
| `blocked_reason` | Why sound comparison could not run, otherwise `null` |

Each retained mismatch contains:

| Field | Meaning |
|---|---|
| `args` | Human-facing selected input |
| `canonical_args` | Typed graph used for input identity and evidence hashing |
| `replay_args` | Alias-aware encoded literal for exact durable replay, or `null` when unsupported |
| `base`, `candidate` | Paired canonical outcome envelopes |
| `artifact` | Embedded source-bound divergence card |

`mismatch_count` still counts every observed difference before canonical
selection. Base observations are converted to candidate-independent structural
values before candidate import. Base and candidate observations contain `kind`,
`return_value`, `exception`, and `mutated_arguments`; exception replay identity
also includes the terminal source location. Canonical observation fields retain
the typed structural graph; display fields never substitute target equality or
repr. Opaque or otherwise non-lossless values set `blocked_reason` and make the
function `inconclusive`.

## Embedded divergence evidence

Each runtime mismatch embeds schema `ordeal.divergence-evidence/v1`:

| Group | Selected fields |
|---|---|
| `revisions` | Commit, callable target, source SHA-256, and source location |
| `comparison` | Comparator/normalizer source binding and tolerances |
| `witness` | Original input, selected canonical input, hashes, and witness source |
| `observations` | Paired JSON-safe base/candidate envelopes |
| `replay` | Attempts, exact matches, signatures, and match basis |
| `minimization` | Canonical observed-case shrinking method and sample boundary |
| `differences` | Changed outcome-envelope channels |
| `boundaries` | What the evidence establishes and does not establish |

`artifact.status = supported` only when minimization is verified, every
immediate replay matches, and revision, comparator, and normalizer source
bindings are complete. Otherwise it is `exploratory`, and an unverified runtime
mismatch makes the function `inconclusive`.

## Status and exit code

| Result status | Exit |
|---|---:|
| `no_divergence_observed` | `0` |
| `divergent` | `1` |
| `inconclusive` | `2` |

See [Compare Two Git Revisions](../guides/revision-diff.md) for the first run and
[Revision Diff Troubleshooting](../guides/revision-diff-troubleshooting.md) for
failure recovery.
