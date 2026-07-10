---
title: Differential Testing, Without Jargon
description: Understand how ordeal compares two implementations without claiming too much.
---

# Differential testing, without jargon

Imagine two cashiers processing the same basket. Looking only at the final total
is not enough. One cashier might reject the basket, remove an item, leave the
till in the wrong state, or quietly write the wrong audit entry.

Differential testing asks a simple question:

> Given the same starting situation, can we observe any difference between
> implementation A and implementation B?

Ordeal turns that question into many small experiments.

## Why each side gets its own basket

Suppose A sorts a list in place. If B receives that already-sorted list, the
comparison is unfair: A changed B's starting point.

Ordeal therefore reconstructs the inputs separately. Bound methods also get
separately reconstructed receiver objects. Selected external effects are reset
to the same baseline before each side runs.

If something cannot be reconstructed safely, ordeal says `inconclusive`. It
does not share the object and hope for the best.

## The outcome is more than a return value

Ordeal compares an **outcome envelope**:

| Part | Plain meaning |
|---|---|
| Return value | What came back normally |
| Exception | Exact exception type and message |
| Mutated arguments | How input objects looked after the call |
| Receiver state | How the bound object looked after the call |
| Selected side effects | External state you explicitly asked to observe |

An `AssertionError` raised by user code is an exception outcome like any other.
It is not mistaken for ordeal's private mismatch signal or silently swallowed.

## What the four statuses mean

| Status | What ordeal can honestly say |
|---|---|
| `divergent` | One concrete input repeatedly produced different envelopes |
| `no_divergence_observed` | The sampled inputs matched; more inputs may still differ |
| `proven_equivalent` | An explicit full-domain verifier supplied by you succeeded |
| `inconclusive` | Isolation, comparison, minimization, or replay was not trustworthy |

`no_divergence_observed` is useful evidence, but it is not proof of equivalence.
Testing 100 green examples cannot rule out example 101.

## From a large failure to one useful witness

When ordeal sees a difference, Hypothesis searches for a smaller input that
still exposes it. Ordeal keeps only the final minimized candidate, then runs
that exact candidate again.

Only a stable replay becomes a `divergent` result with a `DiffWitness`. If the
same envelope cannot be reproduced, the result is `inconclusive` and carries no
witness. This prevents a lucky or timing-dependent observation from becoming a
confident bug claim.

The returned witness is immutable: it is evidence to inspect and preserve, not
a mutable scratchpad containing intermediate shrinking attempts.

## What ordeal does not observe automatically

Ordeal cannot guess which database row, log, metric, message queue, or file is
part of your contract. Select those channels explicitly with `SideEffect`.
Unselected effects remain outside the claim.

Likewise, a divergence says the versions differ; it does not say which version
is correct. Product requirements or an oracle decide that.

## Choose the path that matches your change

- Two Python functions: [Differential Quickstart](../guides/differential-quickstart.md)
- Mutable objects and external effects: [State and Side Effects](../guides/differential-state-and-effects.md)
- Statuses, witnesses, replay, and JSON: [Differential Evidence](../guides/differential-evidence.md)
- Why the durable card binds sources and claims: [Divergence Evidence](divergence-evidence.md)
- Two committed Git revisions: [Revision Diff](../guides/revision-diff.md)
- Why stateful comparison needs a shared story: [System Differential Testing](system-differential.md)
- First stateful service or class comparison: [Your First System Comparison](../guides/system-differential.md)
- A whole module replacement: [Base-to-Candidate Migration](../guides/migration-workflow.md)
