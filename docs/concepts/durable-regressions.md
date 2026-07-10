---
title: Fix a Bug Once
description: A plain-language explanation of durable regressions, evidence cards, and the Ordeal failure loop.
---

# Fix a bug once

A bug report says, “something broke.” A durable regression says, “this exact
thing broke, here is the smallest repeatable example, and our tests will stop it
from returning.”

Think of a leaky roof. Finding the wet floor is discovery. Reproducing the leak
with a hose proves it is not a one-off. Narrowing the spray to one tile makes the
repair understandable. Marking that tile lets you check the repair. Adding it to
the inspection checklist keeps future work from reopening the leak.

Ordeal applies the same loop to software.

## The six-step loop

| Step | Plain meaning | Evidence Ordeal keeps |
|---|---|---|
| Discover | Observe a failure or suspicious counterexample | Bounded claim and code subject |
| Reproduce | Run the same input again | Exact match counts |
| Minimize | Remove irrelevant input or steps | Method, size comparison, and limits |
| Save regression | Turn the witness into a pytest test | Test name, file, and semantic binding |
| Verify fix | Run that same test after the change | Passed, still reproduced, or error |
| Guard CI | Keep every saved regression green | Read-only manifest check |

The shortest command path is:

```bash
ordeal scan myapp.scoring --save-artifacts
# fix the code
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci
```

## What is a witness?

A witness is the concrete input that exposed the problem. “Division may fail”
is a theory. `{"a": 1, "b": 0}` producing `ZeroDivisionError` is a witness.

Ordeal hashes the canonical witness so later tools can tell whether they are
talking about the same input. The hash detects change; it does not explain the
cause or prove the input is the only failing one.

## Why replay before saving?

Some failures depend on timing, random state, or outside services. A failure
that appeared once is useful, but weaker than one that matched on every replay.
Ordeal reports the counts instead of hiding that uncertainty.

- `supported`: the bounded defect claim met its replay and promotion rules;
- `exploratory`: useful evidence exists, but the defect claim is not yet strong;
- `expected`: the input triggered a documented precondition, not a defect.

These labels describe evidence strength. They are not severity ratings.

## What does “minimized” mean?

Minimization makes a failure easier to understand and cheaper to keep. For a
function property, Hypothesis searches within the declared input strategies.
For a stateful trace, Ordeal removes steps and fault toggles while replaying.

“Minimized” does not mean mathematically smallest among every possible input.
The evidence card names the method and preserves this boundary.

## What becomes permanent?

Commit these two files:

```text
tests/test_ordeal_regressions.py
tests/ordeal-regressions.json
```

The Python file contains executable regressions. The JSON manifest binds each
finding ID to its test and the Python structures that affect it. Ordinary
pytest guards behavior; `ordeal verify --ci` also guards the bindings.

The richer `.ordeal/findings/` dossier is local evidence and may stay ignored.

## What this proves, and what it does not

A passing saved regression proves that the recorded witness passes the bound
test in the current checkout. It is not a whole-project correctness certificate.
It does not prove:

- the root cause was correctly diagnosed;
- nearby inputs or untested states are correct;
- dependencies and production infrastructure behave the same way;
- the project has no other bugs.

That narrowness is a feature. A small honest claim is more useful than a broad
claim nobody measured.

## Choose your next page

- Run the loop: [Durable Regression Workflow](../guides/durable-regressions.md)
- Read every card field: [Finding Evidence](../guides/finding-evidence.md)
- Add the guard: [Durable Regressions in CI](../guides/durable-regressions-ci.md)
- Resolve edge cases: [Durable Regression FAQ](../guides/durable-regressions-faq.md)
- Build tooling: [Durable Regression Schema](../reference/durable-regression-schema.md)
