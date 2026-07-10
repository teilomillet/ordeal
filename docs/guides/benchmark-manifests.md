---
title: Maintainer Bug Benchmarks
description: Benchmark Ordeal on public reproductions and private rolling cases without tuning on the reporting set.
---

# Maintainer Bug Benchmarks

Ordinary Ordeal users do not need this workflow. Use
[`ordeal scan`](finding-evidence.md) to get bounded evidence on your own code.
This page documents the maintainers' external regression and evaluation track.
`ordeal benchmark --bug-manifest` scores the real `ordeal scan --json`
workflow against curated bugs and fixed negative controls.

The checked-in public track pairs each Python 3.12+ bug reproduction with a
fixed sibling from the same upstream patch. All three pairs have executable,
source-hashed [evidence records](bug-evidence-records.md). Broad certification
stays disabled because three public cases do not estimate general accuracy.

## Starter Manifests

- [Public track](https://github.com/teilomillet/ordeal/blob/main/benchmarks/bug-benchmark.public.toml)
- [Private template](https://github.com/teilomillet/ordeal/blob/main/benchmarks/bug-benchmark.private.template.toml)
- [Selection ledger](https://github.com/teilomillet/ordeal/blob/main/benchmarks/bug-benchmark.cases.md)

Each `[[cases]]` entry describes one scored surface:

- `dataset`: corpus or reproduction label
- `tier`: cohort such as `public` or `private`
- `workspace`: checkout or fixture directory
- `module`: Python module to scan
- `targets`: optional `ordeal scan --target` selectors
- `expected_outcome`: `bug` for a positive or `clean` for a negative control
- `pair_id`: joins one buggy surface to its fixed sibling
- `expected_targets` or `expected_files`: the scope in which a finding counts
- `evidence_path`: optional executable evidence record linked to the pair
- `expected_error_type`, `expected_error_message`, and
  `expected_witness_sha256`: exact positive-case oracle values
- `requires_python`: supported runner range, such as `>=3.12`
- `oracle_python_version`: original upstream runtime, stored as provenance

Each case must also declare its epistemic basis:

- `selection_reason`: why the case belongs in the benchmark
- `oracle_source`: what makes the ground truth credible
- `oracle_url`: immutable HTTPS source containing `fix_commit`
- `evidence_level`: how strongly the case is verified
- `saturation_risk`: `public`, `private`, or `unknown`
- `allowed_for_optimization`: whether tuning on the case is allowed

The runner rejects public optimization cases, invalid outcomes, and invalid
Python constraints. Certification fails unless every case has a locally
verified record, a passing manifest binding, and full verification; records
requiring online sources must fetch and hash them in that run. Legacy
`python_version` remains an exact runner-version constraint.

## Optional Aggregate Contract

Add `[certification]` only after every case has independently verified evidence:

```toml
[certification]
confidence_level = 0.95
min_positive_cases = 3
min_negative_cases = 3
min_recall = 1.0
min_precision = 1.0
min_specificity = 1.0
min_confidence_bound = 0.40
```

This aggregate is self-attested and claim-scoped. It is not third-party
certification. The checked-in public manifest sets `enabled = false`: its three
verified public pairs support narrow regression claims, not a broad accuracy
claim.

## Run The Public Track

```bash
ordeal benchmark \
  --bug-manifest benchmarks/bug-benchmark.public.toml \
  --benchmark-tier public \
  --online-sources \
  --output-json showcase/bug-benchmark-public.json \
  --check
```

A passing run reports three true positives, three true negatives, and no
incomplete cases. These are observed values for six curated cases, not an
estimate of general bug-finding accuracy.

Verify the hardened HTTPie evidence independently:

```bash
ordeal benchmark \
  --verify-evidence benchmarks/evidence/httpie-3.toml \
  --online-sources \
  --output-json httpie-3-evidence.json
```

Verification fetches pinned authoritative bytes, checks their SHA-256 and size,
checks the local fixtures, and replays both outcomes in fresh processes.

Original BugsInPy checkouts remain supported with `dataset = "bugsinpy"`,
`project`, `bug_id`, and `--bugsinpy-root`. Incompatible historical Python
requirements block instead of becoming misleading misses.

## Run A Private Track

```bash
ordeal benchmark \
  --bug-manifest benchmarks/bug-benchmark.private.template.toml \
  --benchmark-tier private \
  --output-json showcase/bug-benchmark-private.json \
  --check
```

Private cases should point to recent local checkouts. Pair each buggy checkout
with its fixed commit and add it only after both failure commands are verified.

## Scoring

- A `hit` is a true positive on a declared bug.
- A `miss` is a false negative on a declared bug.
- A `correct_rejection` is a fixed control with no scoped finding.
- A `false_positive` is a scoped finding on a fixed control.
- A `blocked` case could not satisfy its workspace or interpreter contract.
- An `error` failed before producing a scoreable scan result.
- `--check` requires certification only when `[certification]` is enabled.
JSON includes raw scans, exact evidence bindings, metrics, and limitations;
public cases are report-only, while private cases are for optimization.
