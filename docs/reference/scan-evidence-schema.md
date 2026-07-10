---
title: Scan Evidence Schema
description: Technical reference for scan findings, replay identity, proof bundles, and harness replay fields.
---

# Scan Evidence Schema

This page describes the fields used by text reports, JSON agent output, saved
bundles, `.proofs.json`, and evidence cards. Missing data remains missing; ordeal
does not infer a successful check that did not run.

## Evidence card

The card schema is `ordeal.finding-evidence/v1`.

| Field | Meaning |
|---|---|
| `status` | `supported`, `exploratory`, or `expected` |
| `claim` | Smallest statement justified by this observation |
| `subject.target` | Fully qualified callable or Compose project/file |
| `subject.source_sha256` | Hash of inspected callable source, or `null` |
| `subject.trace_sha256` | Canonical Compose trace hash, when applicable |
| `witness.input` | JSON-safe exact input |
| `witness.sha256` | Canonical JSON hash of that input |
| `observation` | Exception, contract, or property result |
| `replay` | Attempts, exact matches, basis, and command |
| `minimization` | Performed shrink method and complexity comparison |
| `post_fix_control` | Pending/passed/failed same-witness check |
| `regression` | Generated/saved/not-ready state and binding |
| `ci_guard` | Whether portable verification is ready |
| `boundaries` | Explicitly unsupported conclusions |

Compose uses the same card schema and adds `reliability_coverage` plus
`test_protection`. Partial real-service replay uses `supported`; it never turns
an exact action trace into a deterministic scheduling claim. See the
[Compose evidence loop](../guides/compose-evidence-loop.md).

## Crash replay identity

New crash evidence uses this basis:

```text
same exception type + same message + same terminal source location
```

The terminal location is the final traceback frame’s resolved filename, line,
and function name. This distinguishes two code paths that raise identical text.
`replayable` is true only when every immediate attempt matches. The default is
two attempts.

`replay.match_basis` states the basis actually recorded. Legacy artifacts that
lack the new field retain the older type-and-message description.

## Callable source binding

`subject.source_sha256` hashes `inspect.getsource()` for the unwrapped callable.
It detects a changed callable body. It does not hash dependencies, interpreter
state, environment variables, files read at runtime, or the repository commit.

## Proof bundle version 2

Important groups are:

| Group | Selected fields |
|---|---|
| `witness` | `input`, `source`, `seed_sources`, `supporting_evidence` |
| `contract_basis` | category, fit, reachability, realism, fixture completeness |
| `confidence_breakdown` | replay and promotion component scores |
| `failure_path` | target, error type/text, short traceback, contract check |
| `minimal_reproduction` | target, command, Python snippet, harness support |
| `reproduction` | replay counts, match basis, failing args, reproduction fields |
| `impact` | bounded summary and witness-aligned sink categories |
| `verdict` | category, evidence class, promotion, demotion reason |

## Bound-method reproduction

`minimal_reproduction` adds:

```json
{
  "direct_call_supported": false,
  "harness_replay_supported": true,
  "harness": {
    "mode": "stateful",
    "owner": "myapp.envs:Env",
    "method": "rollout",
    "factory": "tests.support:make_env",
    "setup": "tests.support:prepare_env",
    "scenarios": ["tests.support:offline_sandbox"],
    "state_factory": "tests.support:make_state",
    "state_param": "state",
    "teardown": "tests.support:close_env"
  }
}
```

`harness_replay_supported = false` means at least one required symbol was not
stably resolvable. In that case the report may keep a note and command, but the
regression generator must not emit an invalid bound-method test.

## Promotion fields

- `contract_fit`: agreement with inferred types, shapes, and examples.
- `reachability`: strength of the input source, such as fixture/call-site versus
  unconstrained random generation.
- `realism`: semantic plausibility for the parameter role.
- `fixture_completeness`: whether required runtime pieces were available.
- `impact.critical_sinks`: only sinks aligned with the concrete witness.
- `impact.callable_sink_categories`: broader source-level callable surface.

Promotion is not severity certification. Read `verdict.demotion_reason` whenever
`promoted` is false.

## Artifact and regression bindings

Saved findings add a stable `finding_id`, fingerprint, regression test name, and
AST/import binding. `ordeal verify` checks the binding before pytest. A changed
test is not accepted as the same-witness post-fix control.

See [Finding Evidence](../guides/finding-evidence.md) for interpretation and
[Bug Bundle](../guides/bug-bundle.md) for paths and lifecycle.
