---
title: Safe Module Migrations
description: Why matching the old code is not enough when replacing a module.
---

# Safe Module Migrations

Imagine replacing a shop's checkout calculator. You give the old and new
calculators the same baskets, and both produce the same totals. That is useful:
the replacement probably did not change ordinary behavior.

But suppose the old calculator gives a free order when quantity is negative.
If the new calculator copies that result perfectly, the two versions match and
the bug survives. **Same behavior does not mean correct behavior.**

`ordeal migrate` addresses both questions:

1. What changed between the old module and its replacement?
2. Is the replacement independently checked and protected against mistakes?

## The seven checks, in plain English

| Stage | Question it answers |
|---|---|
| Audit the base | How well did the old tests actually protect the old code? |
| Mine the candidate | What patterns does the new code appear to follow? These are clues, not rules. |
| Diff both modules | Which sampled inputs, outputs, exceptions, names, or signatures changed? |
| Classify changes | Which differences were planned and approved by a human? |
| Save surprises | Can every unexpected difference be rerun as a regression? |
| Mutate the tests | Do the new tests notice deliberate, realistic code breakage? |
| Scan the candidate | Does the replacement reveal problems even when the old module is ignored? |

The order matters. A diff finds change, but explicit invariants say what must be
correct. Mutation checks whether the tests enforce those invariants. The final
scan gives the candidate an independent examination.

## What is an explicit invariant?

An invariant is a rule from the problem domain, written by someone who knows
what the software must do. Examples:

- a risk score stays between 0 and 1
- a refund never exceeds the original payment
- a path never escapes its allowed directory
- a successful response always contains an order ID

Ordeal can mine likely patterns, but it cannot infer business truth. A mined
pattern is labeled `mined_hypothesis`; it never silently becomes an invariant.

## Reading the final result

`PROTECTIVE_WITHIN_MEASURED_SCOPE` means all required stages completed, no
unexpected or inconclusive difference remains, every intended behavior change
has an invariant or callable-attributed mutation protection, every measured
mutant was caught, and a non-empty candidate-only scan passed.

`INCOMPLETE` does not automatically mean the candidate is bad. It means the
evidence is not strong enough yet. A stage may have failed, been blocked, or
left a difference unclassified.

`BLOCKED` on mutation usually means an unexpected regression still fails. Fix
the candidate or explicitly approve the change, then rerun the same workflow.

Even the strongest result is bounded evidence. It does not prove the module
universally correct, race-free, or fast under every workload.

## Pick the right comparison tool

| Your situation | Start here |
|---|---|
| Two functions in one process | [`diff(old, new)`](../guides/differential-quickstart.md) |
| One target across Git commits | [`ordeal diff`](../guides/revision-diff.md) |
| A whole old module and replacement module | [`ordeal migrate`](../guides/migration-workflow.md) |
| A multi-step service or stateful system | [System differential testing](system-differential.md) |

Next: run the [base-to-candidate migration guide](../guides/migration-workflow.md).
