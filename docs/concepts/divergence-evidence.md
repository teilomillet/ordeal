---
title: Divergence Evidence, Explained Simply
description: Why comparing two versions needs a witness, replay, and honest limits.
---

# Divergence evidence, explained simply

Imagine you rewrote a delivery-price function. The old version is trusted; the
new version is faster. Both pass the tests. You still want to know: **did the
rewrite change anything users can observe?**

Ordeal asks both versions the same question:

```text
same input ──→ old revision ──→ old observation
           └→ new revision ──→ new observation
```

If the observations differ, one input has disproved parity. That input is a
**witness**: a concrete example another person can run and inspect.

If 1,000 inputs match, the conclusion is weaker. Ordeal says
**no divergence observed**, not “equivalent.” An untested input may still expose a difference.

## What counts as an observation?

A function can change more than its return value. Ordeal can compare:

- the returned value or raised exception;
- arguments mutated during the call;
- state left on a bound object;
- selected external effects that you teach it to capture and restore.

For Git revision diff, the base generates inputs and the candidate replays those
exact inputs in a separate worktree and process.

## The referee and the translator

The **comparator** is the referee: it decides whether two observations count as
the same. Exact equality is the default; tolerances can accept tiny numeric
drift; a custom comparator can focus on fields that matter.

The **normalizer** is a translator used before the referee. It can remove
irrelevant noise, such as request IDs or differently ordered keys. Because a
normalizer can hide a real change, the artifact records and source-binds it.

## Why bind the source?

A witness without source identity is like a lab sample without a label. Later,
you would not know which code produced it. Each artifact records both revision
identities, callable locations, and SHA-256 hashes of inspectable source.

Hashes detect change and correlation. They are not cryptographic signatures,
authorship proof, or a guarantee that the source is safe.

## Why replay?

One disagreement may be random noise. Ordeal immediately runs the same witness
again and records `exact_matches / attempts`.

```text
3 / 3  same paired observations returned each time
1 / 3  the disagreement was unstable
```

“Exact” refers to the recorded match basis. Exception replay includes type,
message, and terminal source location where available. A stable mismatch with
complete bindings becomes `supported`; unstable or partially bound evidence
stays `exploratory` or makes the overall run `inconclusive`.

## The evidence boundary

Every card says both what it establishes and what it does not. A supported card
establishes a difference for the recorded input, sources, comparison rules, and
measured runtime. It does **not** establish:

- which version is correct;
- the root cause;
- behavior for untested inputs or hidden side effects;
- general equivalence when no mismatch was found.

That boundary is not a disclaimer added at the end. It is part of the machine
record so tools and people inherit the same honest claim.

## From story to action

1. Compare the two versions.
2. Read the smallest witness before reading the whole diff.
3. Check both source bindings and comparison rules.
4. Read replay counts and the evidence boundary.
5. Decide whether the change is expected.
6. Turn unexpected behavior into a durable regression before fixing it.

Continue with the [hands-on workflow](../guides/divergence-evidence.md), use the
[troubleshooting guide](../guides/divergence-evidence-troubleshooting.md) when
evidence is inconclusive, or inspect the
[exact schema](../reference/divergence-evidence-schema.md).
