---
title: Compare Two Git Revisions
description: Check a committed Python refactor in isolated worktrees, then read the result honestly.
---

# Compare two Git revisions

!!! quote "Plain meaning"
    Imagine putting the old code and new code in two sealed rooms. The old code
    creates test cards; the new code receives copies of those exact cards.
    Ordeal reports anything they do differently.

Use this after a committed refactor, optimization, or dependency upgrade when
both versions live in Git. For two functions already importable together, use
the [function quickstart](differential-quickstart.md) instead.
Read [Divergence Evidence](../concepts/divergence-evidence.md) to understand why
each runtime mismatch binds both commits, comparison rules, observations, and
replay counts; use the [artifact workflow](divergence-evidence.md) afterward.

## Run the first comparison

If the file is `mypkg/scoring.py`, its module target is `mypkg.scoring`:

```bash
ordeal diff mypkg.scoring \
  --base-ref origin/main \
  --candidate-ref HEAD
```

- **base** is the behavior you are comparing against.
- **candidate** is the committed change you want to inspect.
- `HEAD` means committed files only. Uncommitted edits are not included.

Ordeal performs this flow:

```text
base ref      → detached worktree → generate inputs → record outcomes
                                             │
                                             └─ exact same inputs
candidate ref → detached worktree ─────────────→ record outcomes
                                                       │
                                      compare + replay ─┘
```

Each revision runs in its own subprocess. Temporary worktrees are removed when
the command finishes, including after a worker error.

## Read the first line first

| Result | Plain meaning | Exit |
|---|---|---:|
| `DIVERGENT` | A public function changed, or a runtime difference has complete source bindings and matched every immediate replay | `1` |
| `NO DIVERGENCE OBSERVED` | The generated cases matched; untested cases may still differ | `0` |
| `INCONCLUSIVE` | Import, input generation, isolation, or replay was not trustworthy | `2` |

`NO DIVERGENCE OBSERVED` is useful evidence, not proof of equivalence. A
divergence proves that the recorded versions differ for the recorded input; it
does not decide which version is correct.

For module targets, ordeal also compares public function names and signatures.
For calls, it compares return values, exact exception type/message, and mutated
arguments. Unbound instance methods need an object harness and fail closed as
`INCONCLUSIVE`.

## Save a review handoff

```bash
ordeal diff mypkg.scoring \
  --base-ref origin/main \
  --candidate-ref HEAD \
  --save-artifacts
```

This writes:

```text
.ordeal/diff/mypkg.scoring.json  # exact machine-readable evidence
.ordeal/diff/mypkg.scoring.md    # short human review
```

The JSON binds both commit hashes, callable source hashes, comparison settings,
same-input observations, and replay counts. See the
[revision diff schema](../reference/revision-diff-schema.md) for every field.

## Keep repeatable settings in TOML

```toml
[diff]
target = "mypkg.scoring"
base_ref = "origin/main"
candidate_ref = "HEAD"
max_examples = 200
seed = 42
replay_attempts = 2
save_artifacts = true
```

Now `ordeal diff` is enough. CLI options override TOML. If `base_ref` is
omitted, ordeal tries the remote default branch, common main/master refs, then
`HEAD^`. Use `rtol` and `atol` only for intentional numerical tolerance.

## Know the execution boundary

Both refs are imported and executed. Generated values pass between the two
trusted local workers through a temporary pickle. Run revision diff only on
code you trust, with dependencies for both revisions installed in the launching
Python environment.

If anything is surprising, use [Revision Diff Troubleshooting](revision-diff-troubleshooting.md).
For the broader idea and proof limits, read
[Differential Testing, Without Jargon](../concepts/differential-testing.md).

If both versions are available as factories and the contract is a multi-step
operation-and-fault story, use the Python
[system comparison path](../concepts/system-differential.md) instead.
