---
title: Scan Quickstart
description: Run ordeal scan, understand the result, and save a regression without writing tests first.
---

# Scan Quickstart

`ordeal scan` is the lowest-friction way to ask: **“What breaks if this code is
called with realistic and awkward inputs?”** It reads your package, discovers
callable functions and methods, and exercises them. A normal scan does not write
project files.

Scan imports and executes target code. If that code can send email, mutate a
database, or call production services, run it with fakes or in an isolated test
environment. “Read-only” describes Ordeal's default artifact behavior, not an
arbitrary target's side effects.

## Your first minute

```bash
cd your-project
ordeal scan .
```

Ordeal auto-detects the package and samples representative public callables. If
the project layout cannot be inferred, pass a module or Python file:

```bash
ordeal scan myapp.scoring
ordeal scan myapp/scoring.py
```

Do not start with target inventory or tuning. Use `--list-targets` only if the
scan is blocked or you need to inspect its scope. Narrow a large package only
after the first result:

```bash
ordeal scan myapp.scoring --target normalize
ordeal scan myapp.envs:ComposableEnv.rollout
```

`--target` is repeatable and accepts globs such as `Env.*`. An explicit target
uses `module:callable`; a module scan plus selector uses `--target`.

## Read the result

| Status | Plain meaning | What to do |
|---|---|---|
| `supported` | The same witness reproduced the same recorded failure | Inspect and save it |
| `exploratory` | Something interesting happened, but evidence is incomplete | Review inputs, types, or harness |
| `expected` | The exception matches a documented precondition | Usually no bug fix |
| `blocked` | Ordeal cannot construct enough of the target to make a useful call | Run `--list-targets` and add a fixture or object harness |

“Supported” is intentionally narrow. It does not prove the root cause, all
inputs, all process state, or whole-project correctness. See
[Finding Evidence](finding-evidence.md) for the precise boundary.

## Save a real finding

```bash
ordeal scan . --save
```

When findings exist, this saves a readable dossier, JSON proof data, replay
notes, reviewable config suggestions, and—when an exact witness can be rendered—a
pytest regression. The output prints the paths and one follow-up command.
`--save-artifacts` remains the compatible long spelling of `--save`.

The durable loop is:

```bash
uv run pytest tests/test_ordeal_regressions.py -q  # fails before the fix
# fix the product code
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci
```

Commit the generated pytest file and `tests/ordeal-regressions.json`. The richer
`.ordeal/findings/` history may remain local. See [Bug Bundle](bug-bundle.md).

## When methods need objects

A method such as `Env.rollout()` cannot run without an `Env`. Ordeal looks for
factories, setup hooks, state builders, scenarios, and teardown hooks in nearby
tests, support files, `conftest.py`, docs, and `ordeal.toml`.

```bash
ordeal scan myapp.envs --list-targets
```

If discovery is insufficient, add a reviewed `[[objects]]` block. The complete
lifecycle and exact replay requirements are in
[Object Harnesses and Stateful Replay](scan-object-harnesses.md).

## Useful depth controls

```bash
ordeal scan myapp.scoring -n 200                 # more examples
ordeal scan myapp.scoring --mode candidate       # stricter ranking
ordeal scan myapp.io --security-focus            # trust-boundary bias
ordeal scan myapp.scoring --no-seed-from-tests   # ignore nearby test examples
ordeal scan myapp.scoring --json                 # agent/tool output
ordeal scan . --deepen --time-limit 60           # one safe planned follow-up
ordeal scan . --base-ref origin/main              # prioritize changed operations
```

Broad package scans sample representative exports and cap depth for speed. Use
`--list-targets` and explicit selectors when completeness matters.

Every scan also reports a source-backed reliability map. It connects retry,
fallback, recovery, I/O, transaction, and ML/data seams to candidate properties
and labels each operation × fault × property cell `PASS`, `NOT EXERCISED`, or
`FAIL`. Mined properties are hypotheses. See [Evidence Closure](evidence-closure.md).

## Exit codes

- `0`: no scan findings were counted, or a target listing succeeded.
- `1`: findings or a blocked scan were reported.
- `2`: invalid command usage, such as combining an explicit callable with
  separate `--target` selectors.

For failure diagnosis, use [Scan Troubleshooting](scan-troubleshooting.md). For
machine fields, use the [Scan Evidence Schema](../reference/scan-evidence-schema.md).
