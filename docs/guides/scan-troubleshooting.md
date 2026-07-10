---
title: Scan Troubleshooting
description: Diagnose missing targets, blocked harnesses, exploratory findings, replay gaps, and slow scans.
---

# Scan Troubleshooting

Start every diagnosis with the read-only inventory:

```bash
ordeal scan mypkg --list-targets
ordeal scan mypkg --list-targets --json
```

## “My function is missing”

- Public module functions and public class methods are discovered by default.
- Single-underscore names need `--include-private`.
- Untyped parameters need a fixture or registered strategy.
- Broad package scans may sample exports. Use `--target` for the exact callable.
- Helper/factory functions are deprioritized in broad scans but remain directly
  selectable.

```bash
ordeal scan mypkg.mod --target 'Env.*' --include-private
```

## “This method needs an object factory”

Ordeal found the method but cannot safely create its owner. Read the ranked
hints in `--list-targets`, then add or correct `[[objects]]` in `ordeal.toml`.
Prefer a real test factory over an invented constructor call.

## “The auto-harness dry-run failed”

The discovered factory raised, returned the wrong class, or required pytest
injection unavailable to a normal function call. Move reusable setup into a
zero-argument or optional-instance support function. Keep pytest-only fixtures
as evidence, not as runtime hooks.

## “A finding is exploratory, not supported”

Inspect these fields in the report or JSON:

- `replay`: did every immediate replay match?
- `contract_fit`: does the input fit hints and observed examples?
- `reachability`: did it come from tests, fixtures, call sites, or only fuzzing?
- `realism`: does the value match the parameter’s inferred role?
- `verdict.demotion_reason`: which promotion condition failed?

Do not lower thresholds first. Add realistic examples, precise type hints, a
fixture, or an explicit contract, then rescan.

## “Replay says the same message, but still failed”

New crash replays match the exception type, message, and terminal source
location. The same text raised from a different line is a different failure
seam and does not count as an exact match.

Older saved bundles may only record type-and-message matching. Their evidence
card preserves that recorded basis instead of silently upgrading the claim.

## “No regression was generated”

A concrete witness must exist. Bound methods also need stable references for
the owner and every configured lifecycle hook. Replace lambdas and nested local
functions with module-level helpers, then run `--save-artifacts` again.

## “No artifacts were written”

`--save-artifacts` writes only when findings exist. A clean scan prints
`No findings yet; no artifacts written.` This is not an error.

## “Verify refuses to run”

Single-finding verification requires `--allow-unsafe-artifacts` because saved
paths can point pytest at repository code. It also fails closed when the saved
test AST or target-import binding changed.

```bash
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci
```

Regenerate intentionally changed bindings with a fresh
`ordeal scan <module> --save-artifacts`; do not hand-edit hashes.

## “Scan is slow”

- Use one explicit `--target` instead of a package root.
- Lower `-n` while fixing harness/config issues.
- Keep generated data under standard pruned roots such as `.ordeal`.
- Run `--no-seed-from-tests` only when adjacent tests are unusually large and
  their examples are not needed.
- Use `--list-targets` to see whether many methods trigger harness discovery.

Broad package scans already sample exports, cap example depth, and disable
call-site seed mining for speed unless you explicitly narrow the target.

## Still uncertain?

Save JSON and inspect the evidence without parsing terminal prose:

```bash
ordeal scan mypkg.mod --target fn --json > scan.json
```

Use [Scan Quickstart](scan-quickstart.md) for the workflow,
[Object Harnesses](scan-object-harnesses.md) for lifecycle semantics, and the
[Scan Evidence Schema](../reference/scan-evidence-schema.md) for every field.
