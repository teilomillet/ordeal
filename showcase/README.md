# Ordeal Showcase Pack

Generated on April 17, 2026.

This folder is a small, shareable proof pack for Ordeal. It is based on
measured runs in this repository, not hand-written examples. It is useful for
quick demos, release notes, and investor or contributor updates.

It is not an external benchmark. If you want a public score against third-party
software, use this pack as the baseline and add a real benchmark suite on top.

The checked-in benchmark suite now lives outside this folder:

- `benchmarks/bug-benchmark.public.toml`: public modern-reproduction track
- `benchmarks/bug-benchmark.private.template.toml`: private holdout template
- `benchmarks/bug-benchmark.cases.md`: selection ledger and caveats

## What It Shows

- `perf-contract-pr.json`: PR-tier perf and quality contract snapshot.
  Highlights from this run:
  `import_cli` median `0.021s`, `audit_demo_cold` median `0.638s`,
  `audit_demo_warm` median `0.107s`, `mutate_tiny_add` median `0.118s`.
- `ordeal-demo.audit-summary.json`: audit snapshot for `ordeal.demo`.
  In this run the current suite had `5` tests, `32` test lines, and
  `100%` line coverage, but mutation validation still reported
  `22/26 (85%)` and the weakest target was `ordeal.demo.score` at `2/5 (40%)`.
- `ordeal-demo.scan.md` and `ordeal-demo.scan.json`: one saved finding bundle
  from `ordeal scan ordeal.demo --save-artifacts`.
- `ordeal-demo.regressions.py`: generated regression file from the saved scan.
- `ordeal-demo.scan.toml`, `ordeal-demo.proofs.json`, `ordeal-demo.replay.md`:
  review and replay sidecars copied from the scan artifact set.

## Why This Is Useful

- Speed: the perf contract gives concrete latency numbers instead of vague
  claims that Ordeal is "fast enough".
- Honesty: the audit snapshot shows the exact message people remember:
  `100%` coverage did not mean the suite was strong enough.
- Proof: the scan bundle leaves behind a report, JSON evidence, and a runnable
  regression file instead of a screenshot with no reproduction path.

## Refresh The Pack

```bash
.venv-codex/bin/ordeal benchmark --perf-contract ordeal.perf.toml --tier pr --output-json showcase/perf-contract-pr.json
.venv-codex/bin/ordeal audit ordeal.demo --json
.venv-codex/bin/ordeal scan ordeal.demo --save-artifacts
```

Then copy the updated `.ordeal/findings/ordeal/demo.*` files and
`tests/test_ordeal_regressions.py` into this folder again.

Note: `ordeal scan --save-artifacts` returns a non-zero exit code when it finds
issues. For this pack that is the expected outcome.
