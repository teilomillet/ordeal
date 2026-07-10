---
title: Divergence Evidence Schema
description: Exact fields and promotion rules for source-bound divergence cards.
---

# Divergence evidence schema

The schema identifier is `ordeal.divergence-evidence/v1`. One card describes
one behavioral mismatch. Git revision diff retains at most one canonical runtime
card per shared function; a module run can still contain cards for several
functions.

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

| Witness field | Type | Meaning |
|---|---|---|
| `available` | `bool` | Whether a concrete witness was recorded |
| `original_input` | `object` | Human-facing first relevant input |
| `original_canonical_input` | `object` | Typed graph used for original identity |
| `original_sha256` | `str` | Hash of `original_canonical_input` |
| `input` | `object` | Human-facing minimized or selected input |
| `canonical_input` | `object` | Typed graph preserving types, cycles, and aliases |
| `sha256` | `str` | Hash of `canonical_input` |
| `source` | `str` | How the witness was obtained |

Human-facing fields are for inspection; identity and replay use the canonical
fields and hashes.

Each observation records a return or exception plus the measured envelope:

- returned and normalized values, or exception type/message/source location;
- arguments after invocation;
- bound receiver state when measured;
- explicitly selected side effects when measured.

Canonical fields use `ordeal.canonical-observation/v1`: a typed root plus graph
nodes and references that preserve container types, domain-object attributes and
slots, cycles, and alias topology. Human-facing value fields are conveniences;
replay signatures are computed from the canonical fields without calling target
`__eq__` or `repr`. If any selected value has no lossless structural encoding,
the result is `inconclusive` and no supported card is emitted.

Git revision observations use the same `a`/`b` roles for base and candidate.

## Replay and promotion

| Field | Meaning |
|---|---|
| `status` | `verified` when every attempt matched; otherwise `failed` |
| `attempts` | Immediate executions of the same paired witness |
| `exact_matches` | Attempts whose recorded, computed expected, and observed signatures all match |
| `expected_signature` | Hash of the discovery observation pair |
| `observed_signatures` | One signature or `null` per attempt |
| `match_basis` | Exact identity used to count a replay |

A card is `supported` only when replay is verified, source binding is complete,
and minimization is verified. A witness with `minimization.status = not_run`
remains `exploratory` even when every replay matches. Callers must not silently
promote incomplete evidence to a deterministic defect claim.

## Minimization and boundary

In-process `diff()` records Hypothesis shrinking. Git revision diff records
canonical observed-case shrinking: it selects the shortest replay-stable JSON
input from the generated divergent cases. `minimization.boundary` makes clear
that inputs outside the generated sample were not explored.

`boundaries.establishes` scopes the positive claim. `does_not_establish` keeps
root cause, correctness, untested inputs/states, and general equivalence open.

## Persistence

- Python API: always in `DiffResult.artifacts`; `artifact_dir=` writes JSON.
- Revision CLI: embedded in `.ordeal/diff/<target>.json` with `--save-artifacts`.

See the [layman explanation](../concepts/divergence-evidence.md),
[workflow](../guides/divergence-evidence.md), and
[troubleshooting](../guides/divergence-evidence-troubleshooting.md).
