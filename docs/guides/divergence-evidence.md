---
title: Use Divergence Evidence
description: Compare versions, save evidence, and decide what the result means.
---

# Use divergence evidence

This guide starts with the shortest useful commands, then shows how to read and
act on the evidence. For the underlying idea, read
[Divergence Evidence, Explained Simply](../concepts/divergence-evidence.md).

## Choose the comparison surface

Use the Python API when both implementations can be imported together. Use the
CLI when they live at different Git commits.

### Two functions in one process

```python
from ordeal import diff

result = diff(
    old_total,
    new_total,
    max_examples=200,
    replay_attempts=3,
    artifact_dir=".ordeal/divergences",
)

print(result.status)
print(result.summary())
```

Each function receives an isolated copy of the input. When a mismatch exists,
`result.witness` is the minimized immutable example and `result.artifacts[0]`
is its `ordeal.divergence-evidence/v1` card.

### Two committed Git revisions

```bash
ordeal diff mypkg.pricing \
  --base-ref origin/main \
  --candidate-ref HEAD \
  --max-examples 200 \
  --replay-attempts 3 \
  --save-artifacts
```

This writes `.ordeal/diff/mypkg.pricing.json` and `.md`. `HEAD` means committed
`HEAD`; uncommitted working-tree edits are not compared.

## Read the status first

| Status | Plain meaning | Next action |
|---|---|---|
| `divergent` | A replay-supported difference was established | Inspect the witness |
| `no_divergence_observed` | Sampled observations matched | Keep the evidence boundary |
| `inconclusive` | A sound claim could not be made | Fix the stated blocker |
| `proven_equivalent` | A caller-supplied proof accepted the full domain | Review that proof |

Revision CLI exit codes are `0` for no observed divergence, `1` for supported
behavior or public-surface divergence, and `2` for inconclusive evidence.

## Read one artifact in order

Open the JSON and follow this sequence:

1. `claim`: the smallest statement the evidence supports.
2. `revisions`: the exact callables, refs/commits, paths, and source hashes.
3. `comparison`: comparator, normalizer, tolerances, and exception rule.
4. `witness`: original and minimized same-input values.
5. `observations`: what revision A and revision B actually did.
6. `replay`: attempts, exact matches, signatures, and match basis.
7. `boundaries`: what remains unknown.

Do not start by reading thousands of changed source lines. The witness tells you
which behavioral question the source diff must explain.

## Accept intentional differences explicitly

Normalize only irrelevant representation noise, then compare the domain value:

```python
def stable_fields(payload: dict) -> dict:
    return {key: payload[key] for key in ("total", "currency")}

def same_price(left: dict, right: dict) -> bool:
    return left == right

result = diff(
    old_quote,
    new_quote,
    normalize=stable_fields,
    compare=same_price,
)
```

The artifact source-binds both helpers. Prefer a named function over a clever
lambda so reviewers can understand what was ignored and why.

## Turn an unexpected difference into protection

Treat the artifact as the handoff, not the final test:

1. reproduce the same witness outside the broad sample;
2. decide which observation is intended from requirements or an oracle;
3. write a focused regression that fails before the fix;
4. fix production code without changing the witness;
5. prove the regression passes afterward;
6. keep it in CI.

The [Durable Regression Workflow](durable-regressions.md) explains the complete
red-fix-green handoff. The [schema reference](../reference/divergence-evidence-schema.md)
lists every machine field.

## Safety

Both modes execute compared code. Revision diff also imports code from both
commits and transfers generated Python values through a run-owned temporary
pickle. Compare only revisions you trust in the current environment.
