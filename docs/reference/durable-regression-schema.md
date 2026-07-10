---
title: Durable Regression Schema
description: Exact evidence-card, regression-binding, manifest, and verification contracts.
---

# Durable regression schema

This is the machine contract behind saved scan and Compose findings,
single-finding verification, and `verify --ci`.

## Finding evidence card

The shared card identifier is `ordeal.finding-evidence/v1`.

| Field | Meaning |
|---|---|
| `status` | `supported`, `exploratory`, or `expected` |
| `claim` | Smallest statement justified by the observation |
| `subject` | Target plus callable-source or Compose trace/config hashes |
| `witness` | Canonical input or action trace and its SHA-256 |
| `observation` | Exception, contract/property violation, or Compose failure |
| `replay` | Status, attempts, exact matches, basis, command, and boundary |
| `minimization` | Explicit state, method, complexity, and replay counts |
| `contrast` | Passing/failing observations when measured |
| `reliability_coverage` | Optional operation × fault × property matrix |
| `test_protection` | Optional mutation-backed scoped verdict |
| `regression` | Save state, path, test name, and binding |
| `post_fix_control` | Pending/passed/failed/error state and acceptance rule |
| `ci_guard` | Readiness, command, and acceptance rule |
| `workflow` | Discover, reproduce, minimize, save, verify, and guard states |
| `boundaries` | What the evidence does and does not establish |
| `runtime` | Python version and implementation |

Replay states include `verified`, `supported`, `failed`, and `not_run`.
`supported` is useful for probabilistic Compose replay: at least one exact match
was observed, without claiming deterministic reproduction.

## Divergence evidence

The cross-version card identifier is `ordeal.divergence-evidence/v1`.

| Field | Meaning |
|---|---|
| `revisions` | Both refs/commits, callable locations, and source SHA-256 values |
| `comparison` | Source-bound comparator/normalizer and their parameters |
| `witness` | Original and minimized same-input values plus SHA-256 values |
| `observations` | Both full outcome envelopes |
| `replay` | Attempts, exact matches, basis, and paired signatures |
| `minimization` | Method, original observation, and search boundary |
| `boundaries` | What the divergence establishes and leaves open |

Full replay plus complete source binding is `supported`; otherwise it remains `exploratory`.

## Python regression binding

The identifier is `ordeal.regression-binding/v1`.
| Field | Meaning |
|---|---|
| `test_name` | Exact pytest function name |
| `test_ast_sha256` | Semantic AST hash of the test function |
| `import_ast_sha256` | Required top-level import AST hashes |
| `global_names` | Globals or builtins loaded by the test |
| `global_binding_ast_sha256` | Ordered hashes of statements affecting globals |

Formatting changes do not alter AST hashes; semantic or relevant global-binding
changes do. Required imports must remain, while unrelated additions are allowed.

## Compose trace binding

The identifier is `ordeal.compose-regression-binding/v1`.

| Field | Meaning |
|---|---|
| `trace_sha256` | Canonical JSON hash of the exact redacted portable trace |
| `failure_signature` | Discovery failure kind/message/action identity hash |
| `action_count` | Bound lifecycle, fault, and request action count |

Compose promotion requires at least one exact discovery replay. The post-fix
control then runs the same bound trace and accepts only attempts with no failure;
a different failure is not a pass.

## Portable CI manifest

The default path is `tests/ordeal-regressions.json`, with schema
`ordeal.regression-manifest/v1`. Records are a tagged union:

```json
{
  "schema": "ordeal.regression-manifest/v1",
  "regressions": [
    {"finding_id": "fnd_python", "target": "myapp.scoring.divide",
     "test_file": "tests/test_ordeal_regressions.py",
     "binding": {"schema": "ordeal.regression-binding/v1"}},
    {"finding_id": "fnd_compose_abc", "runner": "compose",
     "trace_file": "tests/ordeal-compose-regressions/fnd_compose_abc.json",
     "binding": {"schema": "ordeal.compose-regression-binding/v1"}}
  ]
}
```

`finding_id` must be unique. Python records require `test_file`, `test_name`,
and an AST binding. Compose records require `runner`, `trace_file`, a trace
binding, and a clean replay policy.

## Local history and verification

Scan bundles under `.ordeal/findings/` contain full cards, commands, and latest
verification state. Compose records can be verified directly from the portable
manifest. `verify --ci` reads but never mutates either form.

| Control result | Meaning | Exit |
|---|---|---|
| Python test passed | same-witness control is green | `0` |
| Python test failed | original defect still reproduces | `1` |
| all Compose attempts clean | bound service control is green | `0` |
| any Compose attempt fails | original or another failure remains | `1` |
| binding/path/execution error | control is invalid or unavailable | `2` |

## Compatibility and safety

- Unknown schemas, missing/duplicate IDs, and incomplete records fail closed.
- CI rejects regression traces, tests, and Compose files outside the workspace.
- Hashes detect change; they are not signatures or provenance proof.
- Repository tests and Compose files remain executable and require review.
- Claims remain bounded to the recorded witness, workload, faults, and runtime.

Related: [Divergence Evidence Schema](divergence-evidence-schema.md), [Compose evidence loop](../guides/compose-evidence-loop.md),
[Finding Evidence](../guides/finding-evidence.md),
[Scan Evidence Schema](scan-evidence-schema.md), and
[Durable Regressions in CI](../guides/durable-regressions-ci.md).
