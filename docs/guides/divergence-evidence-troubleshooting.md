---
title: Divergence Evidence Troubleshooting
description: Understand missing, exploratory, unstable, or inconclusive evidence.
---

# Divergence evidence troubleshooting

Start with the status and `reason`, then inspect the specific section named
below. Do not turn an unavailable measurement into a confident conclusion.

## “No divergence observed” sounds like “equivalent”

It is not. The generated sample matched within the selected comparison rules.
Increase relevant examples, improve strategies, add state/effect probes, or use
a domain proof if you need a stronger claim.

## The run is divergent but has no behavioral artifact

A module can diverge because a public function was added, removed, or changed
signature. That surface difference has no same-input runtime observation, so it
is reported directly instead of inventing a behavioral witness.

## The artifact is `exploratory`

Check `source_binding.missing` and `replay`:

- missing revision: source inspection failed for one callable;
- missing comparator/normalizer: the comparison helper was not inspectable;
- replay below attempts: the paired observations changed between executions.

Keep the record, but do not call it a replay-supported defect.

## Replays are unstable

Look for time, randomness, global counters, network calls, unordered data, or
shared external state. A normalizer may remove irrelevant values such as a
request ID, but it must not erase domain behavior merely to make replay green.

If the instability matters to users, it may itself be the defect. Write a
property about permitted variation rather than forcing exact parity.

## `HEAD` does not include my changes

Revision diff compares commits. Commit or stash and create a candidate commit,
then rerun with its ref. This avoids pretending a temporary worktree represents
uncommitted code that was never copied into it.

## Strategies cannot be inferred

Add type hints or a project fixture registry. For CLI revision diff, configure:

```toml
[diff]
fixture_registries = ["tests.ordeal_fixtures"]
```

The base revision owns input generation; the candidate receives those same
serialized inputs.

## Values cannot cross the revision boundary

The worker reports inconclusive when generated inputs or observations cannot be
pickled or represented safely. Replace live resources with small serializable
descriptions or use the in-process API with explicit fixtures.

## Input or receiver reconstruction failed

In-process `diff()` deep-copies arguments and bound receivers so one side cannot
contaminate the other. Teach the object to copy safely, pass a simpler fixture,
or compare through a serializable adapter. Ordeal will not share the object and
claim the executions were isolated.

## A comparator or normalizer raised

Comparison helpers are part of the harness, not target behavior. Test them with
representative outputs. Prefer named, typed functions; keep normalizers small
and make the ignored fields obvious in review.

## The artifact is large

Revision diff records every observed runtime mismatch so evidence counts match
the report. Reduce `max_examples`, narrow the target to one callable, or fix the
first witness and rerun. Do not truncate artifacts while retaining a larger
`mismatch_count`.

## The source hash changed after a fix

That is expected: production source should change. The hash correlates evidence
with the observed revision; it does not require buggy source to remain forever.
Keep the witness stable and convert intended behavior into a reviewed regression.

## Security boundary

Both modes execute target code. Git revision diff imports both revisions and
uses a temporary pickle owned by the run. Only compare trusted code in an
appropriately isolated environment.

Return to the [workflow](divergence-evidence.md), read the
[plain-language model](../concepts/divergence-evidence.md), or inspect the
[exact schema](../reference/divergence-evidence-schema.md).
