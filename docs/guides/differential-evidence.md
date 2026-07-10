---
title: Differential Evidence and Statuses
description: Review statuses, minimized witnesses, replay, artifacts, and proof boundaries.
---

# Differential evidence and statuses

This page answers the review question: “What does this result actually prove?”

## The decision path

```text
Could both sides be reconstructed independently?
  no  -> inconclusive
  yes -> Did any sampled outcome envelope differ?
           no  -> no_divergence_observed
                  or proven_equivalent with an explicit full-domain verifier
           yes -> Did the minimized envelope replay exactly every time?
                    no  -> inconclusive
                    yes -> divergent + one DiffWitness
```

This ordering matters. A first observation is not promoted before isolation,
minimization, and replay are trustworthy.

## Read a `DiffWitness`

| Field | Review question |
|---|---|
| `args` | What smallest input exposed the difference? |
| `outcome_a`, `outcome_b` | What complete envelopes were replayed? |
| `differences` | Which envelope channels differed? |
| `replay_attempts`, `replay_matches` | Did every immediate replay match? |
| `replay_verified` | Is this eligible for `divergent`? |
| `artifact` | What source-bound machine evidence is available? |

A returned divergent witness always has `replay_verified is True`. Results that
cannot meet that invariant are `inconclusive` and expose no witness.

## Why only one witness exists

Hypothesis may encounter many larger failing examples while shrinking. Those
are search mechanics, not durable evidence. Ordeal's private mismatch exception
carries only the final candidate out of Hypothesis. Intermediate candidates are
not collected or returned.

Ordeal then re-executes the minimized input against independently reconstructed
sides. Replay compares the paired return/exception observations, mutated
arguments, receiver states, selected effects, and differing channel names.

Exact observations use one typed structural graph across function, system, and
Git revision diff. Domain objects are read from instance attributes and slots;
their `__eq__` and `repr` are not evidence. A clone that still reaches mutable
source state, or a value that cannot be represented losslessly, makes the run
`inconclusive`. A replay counts only when the recorded expected signature, the
computed expected signature, and the observed signature are identical.

## Save the JSON evidence

Every supported divergence has an in-memory artifact:

```python
result = diff(old, new, artifact_dir=".ordeal/divergences")

artifact = result.witness.artifact
print(artifact["revisions"])
print(artifact["comparison"])
print(artifact["replay"])
```

`artifact_dir` writes the same canonical `ordeal.divergence-evidence/v1` JSON.
The record binds callable source hashes, comparison and normalization semantics,
the minimized input, both observations, exact replay counts, and claim limits.

The artifact says that the versions differ for this witness. It does not say
which version is correct, explain the root cause, validate untested inputs, or
observe side effects you did not select.

To keep the witness fixed, also pass `regression_path=` and `manifest_path=`.
Ordeal writes a source-bound pytest that compares the same importable pair on
the minimized literal input. It fails before the fix, passes when the measured
divergence disappears, and runs through `ordeal verify --ci`.

## `proven_equivalent` is deliberately rare

Ordinary sampling cannot produce this status. It requires
`equivalence_proof=`, a verifier supplied by you that establishes equivalence
for the complete input domain and the whole selected outcome envelope.

```python
result = diff(old, new, equivalence_proof=verify_full_contract)
```

A verifier that checks only return expressions is insufficient when arguments,
receiver state, exceptions, or selected effects can change. When in doubt, omit
the verifier and keep the honest `no_divergence_observed` status.

## Reporting language

Use these sentences:

- `divergent`: “The minimized input reproduced different envelopes in every replay.”
- `no_divergence_observed`: “No difference was observed in N sampled inputs.”
- `proven_equivalent`: “The named verifier established the declared full contract.”
- `inconclusive`: “The comparison could not make a sound claim because …”

Avoid “the refactor is correct.” Parity can preserve the same bug in both
versions, and divergence can be intentional.

For the first runnable example, return to the
[Differential Quickstart](differential-quickstart.md). For exact fields and
types, use the [API Reference](../reference/api.md#diff). For the source-bound
card narrative, continue to [Divergence Evidence](../concepts/divergence-evidence.md),
its [operational workflow](divergence-evidence.md), or the
[exact schema](../reference/divergence-evidence-schema.md).
