---
title: Revision Diff Troubleshooting
description: Fix ref, import, strategy, replay, artifact, and trust-boundary problems.
---

# Revision diff troubleshooting

## My uncommitted change was ignored

`--candidate-ref HEAD` means the committed `HEAD`. Ordeal warns when the current
worktree is dirty. Commit the candidate, or point `--candidate-ref` at another
commit. Stashing alone removes the change from the comparison.

## “could not infer a base revision”

Pass it explicitly:

```bash
ordeal diff mypkg.scoring --base-ref origin/main --candidate-ref HEAD
```

Without `--base-ref`, ordeal tries `origin/HEAD`, `origin/main`,
`origin/master`, local `main`/`master`, then `HEAD^`.

## A worker cannot import my package

Run from the repository root. Both workers use the Python environment that
launched ordeal and prepend the selected worktree plus `src/` to `sys.path`.
Install dependencies required by both commits, then verify the target spelling:

```bash
python -c "import mypkg.scoring"
```

An import failure is `INCONCLUSIVE`, not evidence that behavior matched.

## “cannot infer strategies”

Add parameter type hints or register project-specific Hypothesis strategies.
Load a registry once or repeat the CLI option:

```toml
[diff]
fixture_registries = ["tests.ordeal_fixtures"]
```

```bash
ordeal diff mypkg.scoring --fixture-registry tests.ordeal_fixtures
```

## An instance method is inconclusive

Unbound methods need a receiver. Revision diff currently supports module
functions, directly callable targets, and static methods. It refuses to call
both versions without `self`, because two matching missing-`self` errors are not
behavioral parity. The `blocked_reason` therefore asks for an object
factory/harness. Compare a module-level adapter or static method instead.

## The result is inconclusive after a mismatch

The paired base/candidate observations must match every immediate replay and
their source/comparator bindings must be complete. Stabilize random seeds,
clocks, environment variables, files, network calls, and global state. Increase
`--replay-attempts` only after removing the source of instability.

## A signature change is divergent with zero mismatches

That is expected. Added/removed public functions and changed signatures are
surface divergences; they do not need a generated runtime input. Adapt or
explicitly approve the API change in review.

## `NO DIVERGENCE OBSERVED` sounds cautious

It is intentionally precise. Increase `--max-examples`, improve fixtures, and
target the most important module, but do not rename sampled agreement to
equivalence.

## Artifacts were rejected

`diff.artifact_dir` must stay inside the current workspace. Use a relative path
such as `.ordeal/diff` or `artifacts/diff`. The command creates JSON and Markdown
only with `--save-artifacts` or `save_artifacts = true`.

## Is it safe to compare an untrusted pull request?

No sandbox is promised. Both refs are imported and executed, and the private
workers exchange a temporary pickle. Review or isolate untrusted code before
running the command.

Still stuck? Run `ordeal diff --help`, add `--json`, and inspect `status`,
`candidate_resolution_error`, each function’s `blocked_reason`, and the
[field reference](../reference/revision-diff-schema.md).
