---
title: Bug Bundle
description: Save a shareable finding report, review scaffolds, replay notes, a pytest regression, and scan history from one command.
---

# Bug Bundle

`ordeal scan --save-artifacts` is the shortest path from "interesting bug" to "fixed and locked in".

It does the core four things when `scan` finds something:

- Writes a Markdown dossier under `.ordeal/findings/`
- Writes a machine-readable JSON bundle with stable finding IDs under `.ordeal/findings/`
- Writes or updates `tests/test_ordeal_regressions.py`
- Appends a history entry to `.ordeal/findings/index.json`

When ordeal has enough evidence to scaffold follow-up work, the same command also writes review-first sidecars next to the report:

- `.ordeal/findings/<module>.ordeal.toml`
- `.ordeal/findings/<module>.ordeal_support.py`
- `.ordeal/findings/<module>.proofs.json`
- `.ordeal/findings/<module>.replay.md`
- `.ordeal/findings/<module>.scenarios.md`

## Try it on the demo

Run the built-in demo module first:

```bash
uv run ordeal scan ordeal.demo --save-artifacts
```

You should see a short summary, then an artifact block like:

```text
artifacts:
  report: .ordeal/findings/ordeal/demo.md
  bundle: .ordeal/findings/ordeal/demo.json
  regression: tests/test_ordeal_regressions.py
  config: .ordeal/findings/ordeal/demo.ordeal.toml
  support: .ordeal/findings/ordeal/demo.ordeal_support.py
  proofs: .ordeal/findings/ordeal/demo.proofs.json
  replay: .ordeal/findings/ordeal/demo.replay.md
  scenarios: .ordeal/findings/ordeal/demo.scenarios.md
  index: .ordeal/findings/index.json
available:
  verify: uv run ordeal verify fnd_dcb0fc0808d3
  pytest: uv run pytest tests/test_ordeal_regressions.py -q
  review-config: cat .ordeal/findings/ordeal/demo.ordeal.toml
  review-support: cat .ordeal/findings/ordeal/demo.ordeal_support.py
  rescan: uv run ordeal scan ordeal.demo --save-artifacts
```

## What each artifact is for

- `.ordeal/findings/ordeal/demo.md` is the human-readable bug dossier. Share it in a PR, issue, or LLM handoff.
- `.ordeal/findings/ordeal/demo.json` is the machine-readable bundle. It carries stable `finding_id` and `fingerprint` fields so agents can correlate the same issue across runs.
- `tests/test_ordeal_regressions.py` is the runnable pytest file generated from concrete findings. It should fail before the fix and pass after.
- `.ordeal/findings/ordeal/demo.ordeal.toml` is the ready-to-review config bundle. Copy the parts you want into `ordeal.toml`.
- `.ordeal/findings/ordeal/demo.ordeal_support.py` is the review scaffold for factory/setup/scenario helpers that ordeal inferred from the scanned surface.
- `.ordeal/findings/ordeal/demo.proofs.json` extracts the proof bundle for each finding into a smaller machine-readable artifact.
- `.ordeal/findings/ordeal/demo.replay.md` collects minimal repro commands/snippets for the concrete findings from that scan.
- `.ordeal/findings/ordeal/demo.scenarios.md` lists inferred reusable scenario libraries plus the built-in packs you can use directly in `[[objects]].scenarios`.
- `.ordeal/findings/index.json` is the append-only local history for saved scan runs. It records the module, findings, paths, and suggested commands.

## Read the proof bundle fields

- `impact.critical_sinks` is witness-aligned: it only lists high-risk sinks supported by the failing input.
- `impact.callable_sink_categories` keeps the broader callable-level sink inference when you need the larger surface.
- `verdict.promoted = false` with a `demotion_reason` means ordeal kept the crash exploratory, often because a critical-sink witness did not replay cleanly.
- For the exact promotion and helper-filtering rules, see [Scan Finding Rules](scan-finding-rules.md).

## Close the loop

1. Run the generated regression file:

   ```bash
   uv run pytest tests/test_ordeal_regressions.py -q
   ```

2. Fix the bug in your code.

3. Re-run the regression file:

   ```bash
   uv run pytest tests/test_ordeal_regressions.py -q
   ```

4. Re-scan the module:

   ```bash
   uv run ordeal scan ordeal.demo --save-artifacts
   ```

The goal is simple: the regression turns green, and the next scan has fewer or no findings.

## Optional: verify one finding directly

When you want a precise rerun for one saved finding, usually in automation or an agent handoff, use the stable `finding_id` from the JSON bundle:

```bash
uv run ordeal verify fnd_dcb0fc0808d3
```

## Notes

- `--save-artifacts` only writes files when `scan` has findings.
- Re-running the command updates the regression file without duplicating the same generated test.
- If a finding has no replayable concrete input yet, ordeal still writes the Markdown dossier and index entry.
