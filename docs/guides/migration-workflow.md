---
title: Base-to-Candidate Migration
description: Run, understand, and complete Ordeal's end-to-end replacement gate.
---

# Base-to-Candidate Migration

!!! tip "In one sentence"
    `ordeal migrate` checks whether a replacement module changed unexpectedly,
    whether its tests enforce real rules, and whether the replacement exposes
    problems on its own.

New to this idea? Read [Safe Module Migrations](../concepts/safe-migrations.md)
first. It explains why copying the old behavior can also copy an old bug.

## Before you run it

Ordeal will audit the base before judging the candidate, so a weak or blocked
baseline stays visible instead of becoming an assumed truth.

You need:

- an importable base module, such as `oldpkg.scoring`
- an importable candidate module, such as `newpkg.scoring`
- the base module's existing tests
- at least one explicit invariant describing correct candidate behavior

Use the CLI when your invariants are built-in `[[contracts]]` entries:

```bash
ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml
```

Use Python for a domain-specific rule:

```python
from ordeal import ContractCheck, migrate

score_range = ContractCheck(
    "score stays between 0 and 1",
    predicate=lambda value: 0 <= value <= 1,
    kwargs={"features": [0.2, 0.4]},
)

result = migrate(
    "oldpkg.scoring",
    "newpkg.scoring",
    invariants={"score": [score_range]},
)
print(result.summary())
```

## What the first run tells you

The workflow first asks Ordeal to audit the base, then evaluates the candidate
through the remaining evidence stages. The seven lines always appear in this order:

Mutation evaluates generated parity checks and explicit contracts, not the candidate's normal test suite.
If a saved parity regression fails, mutation is `BLOCKED`; Ordeal
still goes on to scan the candidate independently for unrelated findings.

```text
PASSED  audit base: ...
PASSED  mine candidate contracts: ...
PASSED  diff base/candidate: ...
PASSED  classify intended changes: 0 intended, 1 unexpected
PASSED  save unexpected divergences: 1 replayable case
BLOCKED mutate generated checks: generated parity baseline fails
PASSED  scan candidate: ...
RESULT  INCOMPLETE
```

In plain English: the replacement behaved differently, nobody declared that
change intentional, and Ordeal saved the smallest replayable example. Mutation
is blocked because scoring test strength while a basic regression fails would
be misleading. The candidate scan still runs, so independent findings remain
visible.

## Fix or approve the change

If the difference is a bug, fix the candidate and rerun the same command.

If it is planned, record that decision and protect the changed callable with
an invariant for the new rule:

```bash
ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml \
  --intended-change behavior:normalize \
  --intended-change added:score_batch
```

Selectors can be a function name or `behavior:`, `signature:`, `added:`, or
`removed:` plus the function name. Anything unlisted remains unexpected. An
unrelated invariant cannot protect an intended behavior change; the changed
callable needs its own invariant or fully killed, callable-attributed mutants.

## Know when you are done

`PROTECTIVE_WITHIN_MEASURED_SCOPE` requires all of the following:

- the base audit was not blocked
- no unexpected, inconclusive, or evidence-only divergence remains
- at least one explicit invariant ran
- every intended behavior change has callable-scoped protection
- every measured mutant was killed
- the candidate-only scan exercised at least one callable and passed

This is strong, scoped evidence—not a proof that every possible input,
side effect, race, or performance condition is correct.

## Keep the useful artifacts

- `.ordeal/migrations/<base>_to_<candidate>.json` records decisions and evidence
- `tests/test_ordeal_migration_<candidate>.py` replays unexpected divergences

Commit the generated pytest file when it represents a durable decision. The
richer JSON may remain local or be archived with the code review.

Return values, exact exceptions, and mutated arguments can become executable
regressions. Selected side effects, receiver state, custom comparators, and
custom normalizers stay evidence-only unless you supply a durable explicit
contract; Ordeal blocks the strongest verdict instead of pretending otherwise.

## Go deeper

- [`ordeal migrate` flags](cli.md#ordeal-migrate)
- [Python API](../reference/api.md#migration-workflow)
- [How differential evidence works](differential-evidence.md)
