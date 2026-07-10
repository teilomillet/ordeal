# ordeal

[![CI](https://github.com/teilomillet/ordeal/actions/workflows/ci.yml/badge.svg)](https://github.com/teilomillet/ordeal/actions/workflows/ci.yml)
[![Docs](https://github.com/teilomillet/ordeal/actions/workflows/docs.yml/badge.svg)](https://docs.byordeal.com/)
[![PyPI](https://img.shields.io/pypi/v/ordeal)](https://pypi.org/project/ordeal/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/ordeal)](https://pypi.org/project/ordeal/)
[![License](https://img.shields.io/github/license/teilomillet/ordeal)](LICENSE)

**Find a real failure. Keep it fixed.**

Ordeal scans existing Python code with generated inputs, reproduces failures,
and can turn a supported finding into a pytest regression. The first scan needs
no test code or configuration.

## See it find a bug

Given this function:

```python
# risky.py
def average(values: list[float]) -> float:
    """Return the arithmetic mean."""
    return sum(values) / len(values)
```

Run Ordeal without installing it:

```bash
uvx ordeal scan risky.py
```

The relevant part of the output is:

```text
ordeal scan: risky
  status: findings found
  evidence cards:
    - risky.average [supported]
      claim: The recorded input reproducibly makes risky.average raise
             ZeroDivisionError: division by zero.
      witness: input={"values": []}
      replay: verified (2/2 exact matches)
      boundary: Same exception type, message, and terminal source location.
  next: ordeal scan risky.py --save
```

`supported` is deliberately narrow. It means the same input reproduced the same
recorded failure during immediate replay. It does not prove the root cause,
untested behavior, or that a future fix works.

## Run it on your project

```bash
pip install ordeal                  # or: uv tool install ordeal
cd your-project
ordeal scan .                       # auto-detect; write no project artifacts
```

A normal scan imports and executes target code. Isolate code that can send
email, mutate production data, or call live services.

## Keep the failure fixed

```bash
ordeal scan . --save
uv run pytest tests/test_ordeal_regressions.py -q  # prove it fails before the fix
# fix the product code
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci
```

Commit `tests/test_ordeal_regressions.py` with
`tests/ordeal-regressions.json`. The richer `.ordeal/findings/` review history
may stay local.

The complete beginner workflow is:

```text
scan → save one witness → fix → verify the same witness → guard it in CI
```

## What the result means

| Result | Meaning | Next action |
|---|---|---|
| `supported` | The recorded failure matched during immediate replay | Save, fix, verify |
| `exploratory` | Interesting signal without the same replay support | Investigate; do not call it a proven bug |
| `expected` | The input violated a known precondition | Usually no product fix |
| `blocked` | Ordeal could not construct enough of the target | Inspect targets or add a harness |
| `no findings yet` | Nothing failed in the sampled run | Useful evidence, not a correctness proof |

Each scan also emits a reliability map from source-backed retry, recovery, I/O,
transaction, and ML/data seams to candidate properties. `PASS`,
`NOT EXERCISED`, and `FAIL` describe the observed operation × fault × property
evidence; mined properties remain hypotheses. Use `--deepen --time-limit 60`
for one safe planned follow-up, or `--base-ref origin/main` to prioritize
changed operations. Fault probes close only their narrow operational cell after
the named injection boundary is actually reached.

## Why Ordeal

- **No test boilerplate for the first signal.** Point it at a project, module,
  Python file, or callable.
- **Evidence instead of a generic warning.** A supported finding binds the
  source, exact input, exception seam, and replay result.
- **A bug can become protection.** `--save` produces the review bundle and,
  when the witness is renderable, a durable pytest regression.

Start with the [Scan Quickstart](https://docs.byordeal.com/guides/scan-quickstart/).
If a method needs setup or state, continue to
[Object Harnesses](https://docs.byordeal.com/guides/scan-object-harnesses/).
The [Durable Regression Workflow](https://docs.byordeal.com/guides/durable-regressions/)
and [CI guide](https://docs.byordeal.com/guides/durable-regressions-ci/) cover the
full red-fix-green loop.

<details>
<summary><strong>Advanced workflows</strong></summary>

Use these only when the default scan or your specific goal requires them.

| Goal | Start here |
|---|---|
| Judge whether tests detect real changes | [Test Protection](https://docs.byordeal.com/guides/test-protection/) |
| Write a custom stateful chaos test | [Custom Chaos Tests](https://docs.byordeal.com/getting-started/) |
| Exercise long-lived services | [Service Evidence Loop](https://docs.byordeal.com/concepts/service-evidence-loop/) and [Compose Evidence Loop](https://docs.byordeal.com/guides/compose-evidence-loop/) |
| Put service recovery in CI | [Compose CI and operations](https://docs.byordeal.com/guides/compose-operations/) |
| Compare two functions | [Differential Quickstart](https://docs.byordeal.com/guides/differential-quickstart/) and [Divergence Evidence](https://docs.byordeal.com/concepts/divergence-evidence/) |
| Compare committed revisions | [Revision Diff](https://docs.byordeal.com/guides/revision-diff/), [troubleshooting](https://docs.byordeal.com/guides/revision-diff-troubleshooting/), and [schema](https://docs.byordeal.com/reference/revision-diff-schema/) |
| Compare a stateful refactor | [System Differential Testing](https://docs.byordeal.com/concepts/system-differential/) |
| Replace a module without preserving old bugs | [Safe Migrations](https://docs.byordeal.com/concepts/safe-migrations/) and [Migration Workflow](https://docs.byordeal.com/guides/migration-workflow/) |
| Inspect every command or Python type | [CLI reference](https://docs.byordeal.com/guides/cli/) and [API reference](https://docs.byordeal.com/reference/api/) |

</details>

## AI coding agents

Ordeal ships with [AGENTS.md](AGENTS.md), which teaches coding agents to start
with the same scan-first workflow and keep advanced commands behind an explicit
need.

## Development

```bash
git clone https://github.com/teilomillet/ordeal
cd ordeal
uv sync --locked --extra dev
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

Apache 2.0
