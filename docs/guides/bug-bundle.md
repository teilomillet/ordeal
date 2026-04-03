---
title: Bug Bundle
description: Save a shareable finding report, a JSON bundle, a pytest regression, and scan history from one command.
---

# Bug Bundle

`ordeal scan --save-artifacts` is the shortest path from "interesting bug" to "fixed and locked in".

It does four things when `scan` finds something:

- Writes a Markdown dossier under `.ordeal/findings/`
- Writes a machine-readable JSON bundle with stable finding IDs under `.ordeal/findings/`
- Writes or updates `tests/test_ordeal_regressions.py`
- Appends a history entry to `.ordeal/findings/index.json`

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
  index: .ordeal/findings/index.json
next:
  review: .ordeal/findings/ordeal/demo.md
  run: uv run pytest tests/test_ordeal_regressions.py -q
  after fix: uv run ordeal scan ordeal.demo --save-artifacts
```

## What each artifact is for

- `.ordeal/findings/ordeal/demo.md` is the human-readable bug dossier. Share it in a PR, issue, or LLM handoff.
- `.ordeal/findings/ordeal/demo.json` is the machine-readable bundle. It carries stable `finding_id` and `fingerprint` fields so agents can correlate the same issue across runs.
- `tests/test_ordeal_regressions.py` is the runnable pytest file generated from concrete findings. It should fail before the fix and pass after.
- `.ordeal/findings/index.json` is the append-only local history for saved scan runs. It records the module, findings, paths, and suggested commands.

## Close the loop

1. Run the generated regression:

   ```bash
   uv run pytest tests/test_ordeal_regressions.py -q
   ```

2. Fix the bug in your code.

3. Re-run the regression:

   ```bash
   uv run pytest tests/test_ordeal_regressions.py -q
   ```

4. Re-scan the module:

   ```bash
   uv run ordeal scan ordeal.demo --save-artifacts
   ```

The goal is simple: the regression turns green, and the next scan has fewer or no findings.

## Notes

- `--save-artifacts` only writes files when `scan` has findings.
- Re-running the command updates the regression file without duplicating the same generated test.
- If a finding has no replayable concrete input yet, ordeal still writes the Markdown dossier and index entry.
