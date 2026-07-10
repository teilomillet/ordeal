---
title: Durable Regression Schema
description: Exact evidence-card, regression-binding, manifest, and verification contracts.
---

# Durable regression schema

This is the machine-readable contract behind `scan --save-artifacts`,
single-finding verification, and `verify --ci`.

## Finding evidence card

The card schema identifier is `ordeal.finding-evidence/v1`.

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `supported`, `exploratory`, or `expected` |
| `claim` | `str` | Smallest human-readable claim justified by the observation |
| `subject` | `object` | Qualified target and callable source SHA-256 |
| `witness` | `object` | Availability, canonical input, SHA-256, and source |
| `observation` | `object` | Exception, property violation, or contract violation |
| `replay` | `object` | Status, attempts, exact matches, basis, and command |
| `minimization` | `object` | Status, method, complexity proxy, replay counts, boundary |
| `contrast` | `object` | Passing/failing example counts when measured |
| `regression` | `object` | Save status, path, test name, and binding |
| `post_fix_control` | `object` | Pending/passed/failed/error state and acceptance rule |
| `ci_guard` | `object` | Readiness, command, and acceptance rule |
| `workflow` | `object` | Compact state of all six durable-loop stages |
| `boundaries` | `object` | What the evidence establishes and does not establish |
| `runtime` | `object` | Python version and implementation used for the scan |

Replay statuses are `verified`, `failed`, and `not_run`. Minimization records its
explicit state. Regression states include `not_ready`, `not_saved`, `generated`,
`saved`, and `not_applicable`.

## Workflow object

```json
{
  "discover": "observed",
  "reproduce": "verified",
  "minimize": "verified",
  "save_regression": "saved",
  "verify_fix": "pending",
  "guard_ci": "ready"
}
```

The workflow is only a summary. Read replay counts, binding data, acceptance
text, and boundaries before making a decision.

## Regression binding

The binding schema identifier is `ordeal.regression-binding/v1`.

| Field | Type | Meaning |
|---|---|---|
| `test_name` | `str` | Exact pytest function name |
| `test_ast_sha256` | `str` | Semantic AST hash of the test function |
| `import_ast_sha256` | `list[str]` | Required top-level import AST hashes |
| `global_names` | `list[str]` | Module globals or builtins loaded by the test |
| `global_binding_ast_sha256` | `list[str]` | Ordered hashes of statements affecting those globals |

Formatting and line-number changes do not alter an AST hash. Semantic changes
do. Extra unrelated imports may be allowed, but required imports, global
resolution, relevant top-level statements, and the test body must still match.

## Portable CI manifest

The default path is `tests/ordeal-regressions.json`.

```json
{
  "schema": "ordeal.regression-manifest/v1",
  "regressions": [
    {
      "finding_id": "fnd_abc123",
      "target": "myapp.scoring.divide",
      "test_file": "tests/test_ordeal_regressions.py",
      "test_name": "test_divide_crash_regression",
      "binding": {"schema": "ordeal.regression-binding/v1"},
      "witness_sha256": "<64 hex characters>",
      "source_sha256": "<64 hex characters>"
    }
  ]
}
```

`finding_id` must be unique. CI requires `test_file`, `test_name`, and `binding`.
Hashes preserve correlation; source code may legitimately change after a fix.

## Local bundle and index

The JSON bundle under `.ordeal/findings/` contains full cards, artifact paths,
commands, and the latest verification result. Its append-only index records scan
and verification events. Single-finding verification updates these local files.

CI reads the portable manifest and does not require or mutate local history.

## Verification transitions

| Pytest result | Finding status | Post-fix control | Command exit |
|---|---|---|---|
| test passed | `verified` | `passed` | `0` |
| test failed | `reproduced` | `failed` | `1` |
| execution error | unchanged/error context | `error` when recordable | `2` |

CI returns `0` when all records pass, `1` when any regression fails, and `2`
for manifest, binding, path, or execution errors.

## Compatibility and safety

- Unknown schema versions fail closed.
- CI rejects missing/duplicate IDs and paths outside the workspace.
- Hashes detect structural change; they are not signatures or provenance proof.
- Repository tests remain executable code and require normal review/isolation.
- The bounded claim applies only to the recorded witness and measured runtime.

## Related references

- Human interpretation: [Finding Evidence](../guides/finding-evidence.md)
- Scan proof and harness fields: [Scan Evidence Schema](scan-evidence-schema.md)
- Operational artifacts: [Bug Bundle](../guides/bug-bundle.md)
- CI behavior: [Durable Regressions in CI](../guides/durable-regressions-ci.md)
