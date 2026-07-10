---
title: Find a Real Failure
description: Scan Python code, save one replayable failure, verify the fix, and keep it fixed in CI.
---

# Find a real failure. Keep it fixed.

Ordeal exercises Python code with awkward inputs and realistic failures. When
something breaks, it tries to reproduce the same failure and tells you what to do
next. You do not need to choose among Ordeal's testing techniques first.

## Start here

```bash
pip install ordeal                  # or: uv tool install ordeal
cd your-project
ordeal scan .                       # auto-detect the package; write nothing
```

If auto-detection cannot find the package, pass a module or Python file:

```bash
ordeal scan myapp.scoring
ordeal scan myapp/scoring.py
```

A normal scan does not write project artifacts, but it does import and execute
target code. Isolate code that can send email, mutate production data, or call
live services.

## Keep the failure fixed

When the first scan finds a useful failure, save it and follow the printed command:

```bash
ordeal scan . --save
# fix the product code
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci
```

This is the core workflow:

```text
scan → save one witness → fix → verify the same witness → guard it in CI
```

Commit `tests/test_ordeal_regressions.py` and
`tests/ordeal-regressions.json`. The richer `.ordeal/findings/` review history
may stay local. The [Durable Regression Workflow](guides/durable-regressions.md)
explains why the generated test must fail before the fix and pass afterward.

## Read the result

| Result | Plain meaning | Next action |
|---|---|---|
| `supported` | The same failure matched during immediate replay | Save it, fix it, and verify it |
| `exploratory` | Interesting signal, but replay evidence is weaker | Investigate; do not treat it as a proven bug |
| `expected` | The input violated a known precondition | Usually no product fix |
| `blocked` | Ordeal could not construct enough of the target | Inspect targets and add a test harness |

“Supported” is deliberately narrow: the exception type, message, and terminal
source location matched. It does not prove the root cause or certify the project.

## If the first scan needs help

- [Scan Quickstart](guides/scan-quickstart.md) — the complete first-run path.
- [Object Harnesses](guides/scan-object-harnesses.md) — methods that need setup or state.
- [Scan Troubleshooting](guides/scan-troubleshooting.md) — blocked, noisy, or slow scans.
- [Finding Evidence](guides/finding-evidence.md) — what a bounded claim establishes.

New users can stop here. Everything below is a specialized workflow.

<details>
<summary><strong>Advanced workflows</strong></summary>

### Measure test quality

Use `ordeal audit` for a combined generated-check assessment and `ordeal mutate`
to judge whether selected existing tests catch deliberate changes. Start with
[Test Protection](guides/test-protection.md), then use the
[CI policy](guides/test-protection-ci.md) or [FAQ](guides/test-protection-faq.md).

### Exercise a long-lived service

Start with the [service evidence loop](concepts/service-evidence-loop.md), then
the [Compose quickstart](guides/compose-quickstart.md) and complete
[evidence loop](guides/compose-evidence-loop.md). Then
[Put real Compose recovery in CI](guides/compose-operations.md). Use
[Reliability Coverage](concepts/reliability-coverage.md) to see what ran.

### Validate a refactor or migration

- Functions: [Differential Quickstart](guides/differential-quickstart.md) and
  [Divergence Evidence](concepts/divergence-evidence.md).
- Commits: [Revision Diff](guides/revision-diff.md) with
  [troubleshooting](guides/revision-diff-troubleshooting.md) and its [schema](reference/revision-diff-schema.md).
- Stateful workflows: [System Differential Testing](concepts/system-differential.md).
- Module replacements: [Safe Migrations](concepts/safe-migrations.md) before the
  [Migration Workflow](guides/migration-workflow.md). Parity can preserve an old bug.

### Write custom chaos tests

[Custom Chaos Tests](getting-started.md) introduces faults, rules, and invariants.
Continue with [Writing Tests](guides/writing-tests.md),
[Property Assertions](concepts/property-assertions.md),
[Fault Injection](concepts/fault-injection.md), and
[Coverage Guidance](concepts/coverage-guidance.md).

### Use lower-level tools

- [Explorer](guides/explorer.md) — coverage-guided stateful exploration.
- [Auto Testing](guides/auto.md) — mining, fuzzing, and programmatic scanning.
- [Mutations](guides/mutations.md) — direct mutation testing.
- [Simulation](guides/simulate.md) — deterministic clocks and filesystems.
- [Integrations](guides/integrations.md) — API and optional engine bridges.
- [Philosophy](philosophy.md) and [Core Concepts](core-concepts.md) — the ideas behind the tools.

</details>

## Reference

- [Full CLI reference](guides/cli.md) — every command and expert flag.
- [Configuration](guides/configuration.md) — `ordeal.toml`.
- [API reference](reference/api.md) — Python functions and types.
- [Scan evidence schema](reference/scan-evidence-schema.md) — finding JSON.
- [Durable regression schema](reference/durable-regression-schema.md) — bindings and manifests.
- [Divergence evidence schema](reference/divergence-evidence-schema.md) — comparison artifacts.
- [Test protection schema](reference/test-protection-schema.md) — audit and mutation evidence.
- [Troubleshooting](troubleshooting.md) — cross-workflow problems.
