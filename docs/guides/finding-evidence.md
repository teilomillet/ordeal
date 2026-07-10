---
title: Finding Evidence
description: Read the bounded claim, exact witness, replay result, and post-fix control on every scan finding.
---

# Finding Evidence

`ordeal scan` turns each finding into a compact evidence card. You do not need
to run Ordeal's maintainer benchmarks to use it.

Choose the depth you need:

- Plain language: [Fix a Bug Once](../concepts/durable-regressions.md)
- Copy-paste procedure: [Durable Regression Workflow](durable-regressions.md)
- Common decisions: [Durable Regression FAQ](durable-regressions-faq.md)
- Machine contract: [Durable Regression Schema](../reference/durable-regression-schema.md)

```bash
ordeal scan myapp.scoring
ordeal scan myapp.scoring --json
ordeal scan myapp.scoring --save-artifacts
```

A replay-verified crash reads like this:

```text
evidence cards:
  - myapp.scoring.divide [supported]
    claim: The recorded input reproducibly makes ... raise ZeroDivisionError.
    code binding: callable source sha256=<64 hex characters>
    witness: sha256=<64 hex characters> input={"a": 1.0, "b": 0.0}
    replay: verified (2/2 exact matches)
    minimization: verified via hypothesis.find
    regression: saved: tests/test_ordeal_regressions.py
    post-fix control: pending: ... pass on the same witness.
    CI guard: ready: uv run ordeal verify --ci
    boundary: ... Not: the root cause, untested behavior, or that a future fix works.
```

## What Each Field Means

- **Status** is `supported`, `exploratory`, or `expected`. Supported means the
  exact recorded exception type, message, and terminal source location matched
  in every immediate replay.
  Exploratory findings remain leads. Expected preconditions are not defects.
- **Claim** says only what this finding observed. It does not generalize from
  one input to the whole function.
- **Code binding** is the SHA-256 of the inspected callable source. It detects a
  changed callable body; it does not bind dependencies, interpreter binaries,
  process state, or the whole repository.
- **Witness** is the exact input plus a canonical JSON SHA-256. Empty argument
  dictionaries are valid witnesses.
- **Replay** reports attempts and exact matches from the current scan process.
  `2/2` is repeatability evidence, not proof of root cause.
  For crash findings, a match means the same exception type, message, and final
  traceback filename/line/function. Identical text from another source seam is
  not the same replay.
- **Minimization** names the method and reports a transparent witness-size
  comparison when available. Hypothesis shrinking is bounded by the declared
  strategies; it does not prove that a witness is globally smallest.
- **Regression** says whether the exact witness test is generated or saved and
  carries its test/import AST binding. Unsupported findings stay `not_ready`
  instead of receiving a placeholder test. For bound methods, resolvable
  factory, setup, scenario, state, and teardown hooks are reconstructed first.
- **Post-fix control** stays `pending` until the generated regression passes on
  the same witness after a fix. Saved findings bind the generated test AST and
  target import to SHA-256 values; `ordeal verify` refuses a changed regression.
  The control is `not_ready` if no witness exists.
- **CI guard** becomes `ready` only for a saved, bound regression. The
  provider-neutral `ordeal verify --ci` command checks every saved test and
  binding from `tests/ordeal-regressions.json` without changing the evidence
  history.
- **Boundary** lists the conclusions the evidence does not support.

The JSON card also records the Python version and implementation used for the
scan. Saved Markdown, JSON, proof, and replay artifacts carry the same card.
See the [Scan Evidence Schema](../reference/scan-evidence-schema.md) for every
field and [Object Harnesses](scan-object-harnesses.md) for bound-method replay.

## Close A Finding

```bash
ordeal scan myapp.scoring --save-artifacts
uv run pytest tests/test_ordeal_regressions.py -q  # should fail before the fix
# make the smallest justified fix
ordeal verify <finding-id> --allow-unsafe-artifacts  # same witness must now pass
ordeal verify --ci                                   # read-only repository guard
ordeal scan myapp.scoring                          # look for regressions elsewhere
```

Passing the post-fix regression closes the same-witness control. It does not
prove every neighboring input or state is correct; expand the regression or
run `ordeal mutate` when that broader claim matters.

The rich `.ordeal/findings/` history may stay local and ignored. The generated
pytest file and `tests/ordeal-regressions.json` are the small durable pair to
commit. Their paths are repository-relative, so a teammate or CI runner can
verify them even when the original workspace no longer exists. CI mode refuses
paths that escape the current checkout.

## Design Boundary

The presentation follows ideas documented by Antithesis: make properties
explicit, make findings actionable, and attach concrete reproductions. See its
[property model](https://antithesis.com/docs/concepts/properties_assertions/overview/),
[finding reports](https://antithesis.com/docs/product/reports/findings/), and
[deterministic reproduction model](https://antithesis.com/docs/introduction/how_antithesis_works/).

Ordeal does not claim equivalent infrastructure. A local function scan is not
a deterministic full-system simulation, and it does not provide time-travel
debugging. The evidence card exposes the smaller claim Ordeal actually checked.
