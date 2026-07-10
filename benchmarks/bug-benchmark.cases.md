# Bug Benchmark Cases

This ledger records why the checked-in public track exists and what it means.

## Public Reproduction Track

Source: official `soarsmu/BugsInPy` metadata at commit
`11c5f1eea954a42132cfd06bf257766a7963e0fd`, verified July 9, 2026.

Selection rules:

- concise production bug with a direct upstream regression and fix commit
- deterministic bug/fixed pair restricted to the cited regression domain
- report-only: `allowed_for_optimization = false`
- original Python version retained as provenance, not silently discarded

Checked-in manifest: [bug-benchmark.public.toml](./bug-benchmark.public.toml)

| Pair | Original Python | Evidence level | Upstream fix |
|---|---|---|---|
| `bugsinpy-httpie-3` | `3.7.3` | executable, 30 checked values | `589887939507ff26d36ec74bd2c045819cfa3d56` |
| `bugsinpy-pysnooper-3` | `3.8.1` | executable, 30 checked values | `15555ed760000b049aff8fecc79d29339c1224c3` |
| `bugsinpy-tornado-14` | `3.7.0` | executable, 30 checked values | `1d02ed606f1c52636462633d009bdcbaac644331` |

These are reproductions, not claims that Ordeal runs inside the historical
Python environment. The real CLI scans each bug and fixed sibling on Python
3.12+. Each pair is independently source-hashed and replayed. Broad suite
certification stays disabled because three public cases do not estimate general
accuracy; the six observed outcomes remain regression-specific evidence.

## Private Track

Checked-in template: [bug-benchmark.private.template.toml](./bug-benchmark.private.template.toml)

Private cases are deliberately absent from the public repository.

- A private holdout only counts once buggy and fixed revisions replay locally.
- Keep private or low-visibility cases outside public docs and examples.
- Run `scripts/harvest_private_bug_benchmark_case.py` for each buggy/fixed
  checkout with one `pair_id`, then review `expected_files` manually.
