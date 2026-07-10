---
title: Durable Regressions in CI
description: Add a provider-neutral, read-only guard for every saved Ordeal regression.
---

# Durable regressions in CI

The CI contract is one command:

```bash
ordeal verify --ci
```

It is deliberately provider-neutral. Put the command after dependencies are
installed in any CI system, build script, container, or local pre-merge gate.

## Files CI needs

Commit both files produced by `scan --save-artifacts`:

```text
tests/test_ordeal_regressions.py
tests/ordeal-regressions.json
```

The pytest file checks behavior. The manifest identifies each finding, test
node, witness hash, source hash, and semantic binding. CI does not need the
gitignored `.ordeal/findings/` directory.

## Minimal job step

```bash
python -m pip install ordeal
ordeal verify --ci
```

If the project installs Ordeal another way, keep that setup and use the same
verification command. No provider-specific report format is required.

## What CI mode checks

For every manifest record, Ordeal verifies:

1. the manifest uses `ordeal.regression-manifest/v1`;
2. every finding ID exists once;
3. the test path stays inside the current checkout;
4. the regression file and named test exist;
5. the test AST, relevant imports, globals, and global bindings still match;
6. the bound pytest node passes.

CI mode reads these files but does not update the bundle, finding status, or
history. Use single-finding `ordeal verify <id> --allow-unsafe-artifacts` when
you intentionally want to record a post-fix result.

## Exit codes

| Code | Meaning | Typical action |
|---|---|---|
| `0` | Every bound regression passed | Continue the build |
| `1` | At least one regression test failed | Fix the behavior or investigate a stale expectation |
| `2` | Manifest, binding, path, or pytest execution error | Repair the artifact or environment |

Treat `2` as fail-closed. An unreadable guard is not a passing guard.

## Recommended rollout

1. Generate and review the first durable pair locally.
2. Add `ordeal verify --ci` as a non-blocking observation.
3. Confirm it passes in clean checkouts and contributor forks.
4. Make the command required once the environment is stable.
5. Keep ordinary pytest too; it runs all project tests, not only bound ones.

## Custom manifest path

The default is `tests/ordeal-regressions.json`. For a different layout:

```bash
ordeal verify --ci --manifest quality/ordeal-regressions.json
```

When the manifest lives directly under a directory named `tests`, that
directory's parent is the workspace root. Otherwise, the current directory is
the root. Every referenced test must remain inside it.

## Monorepos

Run from each package root with its own manifest, or point explicitly at each
manifest:

```bash
(cd services/billing && ordeal verify --ci)
(cd services/search && ordeal verify --ci)
```

Keep paths repository-relative. Absolute paths from a developer machine are
not portable and should not appear in the committed manifest.

## When the guard fails

- `Regression manifest not found`: commit the JSON file or pass `--manifest`.
- `schema must be .../v1`: regenerate with the current Ordeal version.
- `missing or duplicate finding ID`: repair or regenerate the manifest.
- `refused ... outside workspace`: replace an escaping or absolute path.
- `binding failed`: the test or a relevant global/import changed; re-scan after review.
- `regression failed`: run the printed pytest node locally and inspect the failure.
- `could not run ... exit N`: fix pytest collection, dependencies, or environment.

## Security boundary

CI mode still executes repository tests. Review contributions as executable
code and use the same isolation you use for pytest. Path confinement prevents a
manifest from selecting a test outside the checkout; it does not sandbox code
inside the checkout.

## What CI mode does not do

It does not discover new bugs, prove the original root cause, or test every
input. Run `scan`, `audit`, `mutate`, and stateful exploration separately when
you need broader evidence.

## See also

- Full workflow: [Durable Regression Workflow](durable-regressions.md)
- Common questions: [Durable Regression FAQ](durable-regressions-faq.md)
- Machine contract: [Durable Regression Schema](../reference/durable-regression-schema.md)
