---
title: Durable Regression FAQ
description: Answers about witnesses, replay, minimization, bindings, verification, and CI.
---

# Durable regression FAQ

## Why should the generated test fail before the fix?

That proves the test can see the observed problem. A test that is already green
cannot demonstrate that the fix changed this behavior.

## Why does the test pass after the fix?

Crash and contract regressions call the target without expecting the old
failure. Property regressions assert the intended relationship. Removing the
defect turns that same witness green.

## Is `supported` the same as “definitely a bug”?

No. It means the bounded defect claim met Ordeal's replay and promotion rules.
Intent, severity, root cause, and neighboring behavior still require review.

## What should I do with an `exploratory` finding?

Treat it as a lead. Improve fixtures or object hooks, replay it, inspect the
contract, or explicitly suppress a noisy inferred property. Do not silently
promote it in a bug report.

## What is an `expected` finding?

The witness triggered a documented precondition, such as rejecting a negative
size with `ValueError`. It is recorded as observed behavior, not a defect.

## What exactly is replay matching?

The evidence card names the basis. New crash findings require the same
exception type, message, and terminal source filename/line/function. Older
artifacts preserve their recorded type-and-message basis. Properties require
the same inferred property to fail on the same input.

## Does minimization prove the witness is globally smallest?

No. Hypothesis shrinks within declared strategies. Trace shrinking removes
steps and faults by replay. Both are useful searches with explicit boundaries,
not mathematical proofs over every possible program state.

## Why keep a witness hash?

It detects whether the canonical input changed between artifacts. A SHA-256
match is an integrity check, not proof of correctness or authorship.

## Why keep a JSON manifest if pytest already runs the test?

Pytest protects behavior. The manifest also binds a stable finding ID to the
named test, relevant imports, globals, and global bindings. `verify --ci`
detects a weakened, redirected, missing, or moved regression before running it.

## Can I edit the generated regression?

Review it, but an intentional semantic edit changes its binding. Re-run
`scan --save-artifacts` after the edit so Ordeal records a new control instead
of pretending it is the original witness test.

## Why does single-finding verification require an unsafe opt-in?

The local index and bundle can direct pytest at repository code. The flag makes
that execution decision explicit. CI mode uses a confined committed manifest,
but it still executes repository tests and should run in normal CI isolation.

## Does CI mode change my files?

No. `ordeal verify --ci` is read-only. Single-finding verification updates the
local bundle and index with the post-fix result.

## Will the manifest work after cloning elsewhere?

Yes, when paths are repository-relative. CI resolves them in the current
checkout rather than trusting the original developer's absolute path.

## What about instance methods and stateful objects?

Ordeal can generate direct regressions when the owner, factory, setup,
scenarios, state builder, and teardown have stable module- or file-backed
symbols. Lambdas and nested locals are not portable. If the harness cannot be
reconstructed honestly, the regression stays `not_ready`. See
[Object Harnesses](scan-object-harnesses.md).

## Why was no regression generated?

Common reasons are a missing concrete witness, failed replay, unsupported
property shape, missing object hooks, or an expected precondition. Read the
post-fix control and skipped-finding message; do not create a placeholder that
cannot reproduce the evidence.

## Should I delete a regression after the bug is fixed?

Usually no. The green test is the durable value. Delete or replace it only when
the product contract changed, the covered code was removed, or a broader test
provably supersedes it. Update the manifest in the same change.

## Does this replace my normal tests?

No. Durable regressions preserve failures Ordeal found. Unit, integration,
contract, end-to-end, mutation, and chaos tests answer broader questions.

## Where should I start?

- Plain language: [Fix a Bug Once](../concepts/durable-regressions.md)
- Commands: [Durable Regression Workflow](durable-regressions.md)
- Evidence fields: [Finding Evidence](finding-evidence.md)
- CI: [Durable Regressions in CI](durable-regressions-ci.md)
- JSON and hashes: [Durable Regression Schema](../reference/durable-regression-schema.md)
