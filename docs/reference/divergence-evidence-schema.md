---
title: Divergence Evidence Schema
description: Exact fields and promotion rules for source-bound divergence cards.
---

# Divergence evidence schema

The schema identifier is `ordeal.divergence-evidence/v1`. One card describes
one behavioral mismatch; a revision run can contain many cards.

## Top-level contract

| Field | Type | Meaning |
|---|---|---|
| `schema` | `str` | Exact schema identifier |
| `artifact_id` | `str` | Stable content-derived `div_...` identifier |
| `status` | `str` | `supported` or `exploratory` |
| `claim` | `str` | Smallest justified cross-version statement |
| `revisions` | `object` | Source bindings for sides `a` and `b` |
| `source_binding` | `object` | `complete` or `partial`, plus missing roles |
| `comparison` | `object` | Comparator, normalizer, and exception rules |
| `witness` | `object` | Original/minimized input and canonical hashes |
| `observations` | `object` | Full outcomes for sides `a` and `b` |
| `replay` | `object` | Counts, signatures, status, and match basis |
| `minimization` | `object` | Method, original observation, and limit |
| `differences` | `list[str]` | Outcome-envelope channels that differed |
| `boundaries` | `object` | Established and explicitly open claims |
| `runtime` | `object` | Python version and implementation |

## Revision binding

Each `revisions.a` and `revisions.b` object records:

| Field | Meaning |
|---|---|
| `target` | Module-qualified callable identity |
| `source_sha256` | SHA-256 of inspectable callable source, or `null` |
| `source_location` | Portable path and starting line when inspectable |
| `role` | `base`/`candidate` for Git revision diff when applicable |
| `ref`, `commit` | Requested ref and resolved commit when applicable |

`source_binding.status` is `complete` only when both revisions, comparator, and
normalizer have source hashes. A hash detects change; it is not a signature or
provenance certificate.

## Comparison pipeline

`comparison.comparator` and `comparison.normalizer` each contain `target`,
`source_sha256`, `source_location`, and `kind`. Comparator kinds include
`exact`, `tolerance`, and `custom`; tolerance records `rtol` and `atol`.

`exception_matching` records how cross-version exceptions are compared.
`replay_matching` records the stricter same-witness replay identity.

## Witness and observations

`witness.original_input` preserves the first relevant input.
`witness.input` is the minimized or recorded replay input. Each has a canonical
SHA-256. `source` names how the input was obtained.

Each observation records a return or exception plus the measured envelope:

- returned and normalized values, or exception type/message/source location;
- arguments after invocation;
- bound receiver state when measured;
- explicitly selected side effects when measured.

Git revision observations use the same `a`/`b` roles for base and candidate.

## Replay and promotion

| Field | Meaning |
|---|---|
| `status` | `verified` when every attempt matched; otherwise `failed` |
| `attempts` | Immediate executions of the same paired witness |
| `exact_matches` | Attempts matching the full expected signature |
| `expected_signature` | Hash of the discovery observation pair |
| `observed_signatures` | One signature or `null` per attempt |
| `match_basis` | Exact identity used to count a replay |

A card is `supported` only when replay is verified **and** source binding is
complete. Otherwise it remains `exploratory`; callers must not silently promote
it to a deterministic defect claim.

## Minimization and boundary

In-process `diff()` records Hypothesis shrinking. Git revision diff records
`method: not_run` because it preserves each deterministic generated case.
`minimization.boundary` states which search space was or was not explored.

`boundaries.establishes` scopes the positive claim. `does_not_establish` keeps
root cause, correctness, untested inputs/states, and general equivalence open.

## Persistence

- Python API: always in `DiffResult.artifacts`; `artifact_dir=` writes JSON.
- Revision CLI: embedded in `.ordeal/diff/<target>.json` with `--save-artifacts`.

See the [layman explanation](../concepts/divergence-evidence.md),
[workflow](../guides/divergence-evidence.md), and
[troubleshooting](../guides/divergence-evidence-troubleshooting.md).
