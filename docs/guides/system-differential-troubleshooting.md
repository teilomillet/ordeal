---
title: System Comparison Troubleshooting
description: Explain surprising system-diff results without overstating them.
---

# System comparison troubleshooting

## “must be a zero-argument factory”

`diff()` needs a fresh old and new system for every replay. Wrap configuration:

```python
diff(lambda: OldStore(config), lambda: NewStore(config), sequence=story)
```

Do not return the same singleton from both lambdas.

## “must support deepcopy”

Operation arguments, fault parameters, return values, and observations are
copied so one version cannot mutate what the other receives. Replace live
connections, locks, generators, and file handles with stable IDs or snapshots.

## State diverges as soon as a fault activates

Automatic state includes public attributes. If `timeout_enabled` is a harness
knob rather than business state, select the contract explicitly:

```python
state=lambda service: {"orders": dict(service.orders)}
```

## Side effects say `NOT CHECKED`

This is intentional. Ordeal cannot safely infer emails, database writes, queue
messages, or network calls. Pass `side_effects=` with an isolated observation.

## Recovery says `NOT CHECKED`

Add a recovery fault event followed by at least one operation. Recognized
recovery actions are `deactivate`, `recover`, `restart`, and `clear`.

## The interface mismatches before operations run

Inspect `result.interface.missing_from_a`, `missing_from_b`, and
`signature_mismatches`. Public dynamic attributes created by the factory are
also part of the surface. Rename or adapt intentional differences explicitly.

## The minimized sequence looks different

Ordeal deletes events only while the exact first mismatch—event and both
observations—remains. `result.original_length` and `minimized_length` show the
reduction. The shorter sequence is the smallest explanation found by this
deletion pass, not a proof that no other minimal sequence exists.

## Replay is less than attempted

The event plan is exact, but external scheduling may not be. Report the counts
as written. Stabilize clocks, random seeds, ports, databases, and background
workers before treating the witness as a durable regression.

## Performance is noisy

Increase `warmup` and `samples`, use a longer representative story, and avoid a
near-zero baseline. For tiny workloads, prefer `max_candidate_seconds` over a
slowdown ratio dominated by measurement noise.

## Behavior passes but performance fails

That is a valid result: semantics matched while the candidate exceeded its
separate budget. Fix or approve the speed change without relabeling behavior as
divergent.

## `no_divergence_observed` sounds cautious

It is intentionally precise. The selected story matched; untested stories are
unknown. Add operations, faults, state, and side-effect probes to widen the
measured boundary.

## Python API or `ordeal diff` CLI?

Use `diff(Old, New, sequence=story)` for two factories and a stateful timeline.
Use `ordeal diff target --base-ref ... --candidate-ref ...` for committed Git
revisions in isolated worktrees. See [Revision Diff](revision-diff.md).

Still unsure? Print `result.summary()`, inspect the
[field reference](../reference/system-differential.md), and run
`catalog()["diff"]` to discover the live API.
