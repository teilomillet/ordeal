---
title: Durable Regression Workflow
description: Discover, reproduce, minimize, save, fix, verify, and guard an Ordeal finding.
---

# Durable regression workflow

Use this guide when you want a failure to become a permanent test, not a temporary
report. New to the idea? Read [Fix a Bug Once](../concepts/durable-regressions.md).

## Before you start

Run from the project root. The target must be importable as a Python module; for
`myapp/scoring.py`, use `myapp.scoring`.

```bash
pip install ordeal
ordeal scan myapp.scoring --list-targets
```

`--list-targets` is optional and useful when methods need factories, setup, state,
scenarios, or teardown hooks.

## 1. Discover and save

```bash
ordeal scan myapp.scoring --save-artifacts
```

This explores the target, replays failures, minimizes supported witnesses, writes
evidence, and creates runnable regressions when it has enough information.

```text
status: findings found
regression: tests/test_ordeal_regressions.py
verify: uv run ordeal verify fnd_abc123... --allow-unsafe-artifacts
```

No regression path means Ordeal could not yet produce an honest executable test.
Read `post-fix control` and the skipped-finding message before fixing code.

## 2. Read the evidence card

Check these fields in order:

1. `claim`: the exact statement supported by this run;
2. `witness`: the input and its hash;
3. `replay`: whether the same failure matched again;
4. `minimization`: what was reduced and by which method;
5. `regression`: generated or saved test and binding;
6. `boundary`: what remains unproven.

Do not treat `exploratory` as confirmed. Reproduce it, improve the harness, or
declare an expected precondition first.

## 3. Prove the regression is red

Before the fix, the generated test should fail:

```bash
uv run pytest tests/test_ordeal_regressions.py -q
```

This is the “red” half of red-green testing. If it passes before the fix, stop:
the test does not protect the failure. If import fails, fix the environment; do
not weaken the assertion merely to make collection succeed.

## 4. Make the smallest justified fix

Change production code, not the witness, unless review shows the finding was
invalid. Keep the input stable so before/after results are comparable.

For a false positive, prefer one of these explicit actions:

- document or configure the expected precondition;
- improve type hints, fixtures, or object hooks;
- suppress the named inferred property or relation;
- record why the finding is outside the intended contract.

## 5. Verify the same witness

```bash
ordeal verify fnd_abc123... --allow-unsafe-artifacts
```

The opt-in is required because an artifact can point pytest at repository code.
Verification checks its binding, runs only the bound test, and records one of:

- `verified` / `passed`: the same-witness control is green;
- `reproduced` / `failed`: the defect still appears;
- `error`: pytest or artifact verification could not complete.

After intentional test edits, re-run `scan --save-artifacts` for a new binding.
Ordeal refuses to call a changed test the same control.

## 6. Commit the durable pair

```text
tests/test_ordeal_regressions.py
tests/ordeal-regressions.json
```

Review the Python test like any test and the manifest as its binding record. The
`.ordeal/findings/` history can stay local unless your team archives it.

## 7. Guard CI and re-scan

```bash
ordeal verify --ci
ordeal scan myapp.scoring
```

CI mode is read-only: it checks every manifest record and bound regression. The
final scan looks for neighboring failures; it does not replace the saved test.

## Team handoff checklist

- The finding ID is in the change description.
- The pre-fix regression failed for the expected reason.
- The fix changed production behavior, not merely the test.
- Single-finding verification passed.
- The Python regression and JSON manifest are committed together.
- The provider-neutral CI command runs on every proposed change.
- Any broader claim is backed by audit, mutation, chaos, or integration evidence.

Go deeper with [Object Harnesses](scan-object-harnesses.md),
[Bug Bundle](bug-bundle.md), [Durable Regressions in CI](durable-regressions-ci.md),
and the [Durable Regression Schema](../reference/durable-regression-schema.md).
