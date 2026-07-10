---
name: ordeal
description: Use when working with Python reliability through ordeal: finding missed bugs, assessing existing tests, bootstrapping starter tests, saving regressions or findings, verifying saved findings, or exploring code that imports `ordeal`.
user_invocable: true
---

# ordeal

Use this file as a capability map, not a prescribed workflow.

High-signal entrypoints:
- `ordeal scan <target>`
- `ordeal audit <target>`
- `ordeal mutate <target>`
- `ordeal init [target]`
- `ordeal verify <finding-id> --allow-unsafe-artifacts`
- `ordeal verify --ci`
- `ordeal explore --runner compose`

Read-first CLI:
- `ordeal scan <target>`: unified bug discovery; `--save-artifacts` writes report, JSON bundle, regressions, and index entries
- `ordeal mine <target>`: discover suspicious properties
- `ordeal audit <target>`: measure gaps in existing tests
- `ordeal verify <finding-id> --allow-unsafe-artifacts`: re-run one bound regression after a fix and record the result
- `ordeal verify --ci`: read-only guard for every regression in `tests/ordeal-regressions.json`
- `ordeal explore -c ordeal.toml`: deeper coverage-guided exploration
- `ordeal explore --runner compose`: long-lived services, worker faults, stateful HTTP requests, and exact traces with repeated replay counts

Test protection interpretation:
- `ordeal mutate <target>` judges the selected existing tests directly
- `ordeal audit <target>` reports protection for generated/migrated checks
- `weak` means a mutant survived, a line was uncovered, or a declared property was unexercised
- `protective_within_measured_scope` means all tested mutants died and measured executable lines were covered; never restate it as universal correctness
- `inconclusive` means required mutation or coverage evidence was unavailable
- `tautological_or_weak` means a property killed none of the measured mutants; it is not formal tautology proof
- 100% line coverage does not override a surviving mutant

Scan interpretation:
- Start new users at `docs/guides/scan-quickstart.md`.
- Use `docs/guides/scan-object-harnesses.md` for bound methods and state.
- Use `docs/guides/scan-troubleshooting.md` before lowering evidence thresholds.
- `supported` crash replay matches type, message, and terminal source location.
- A bound regression is exact only when `harness_replay_supported` is true.
- `scan` reconstructs one object lifecycle per witness; `chaos_for` owns multi-step state reuse.
- Exact JSON and proof fields are in `docs/reference/scan-evidence-schema.md`.

Write-producing CLI:
- `ordeal init [target]`: starter tests plus `ordeal.toml`; `--install-skill`, `--close-gaps`, and `--ci` add extra writes
- `ordeal mutate <target> --generate-stubs PATH`: writes suggested test stubs
- `ordeal replay trace.json --output PATH`: writes replay artifacts

Artifacts:
- `.ordeal/findings/`: Markdown reports, JSON bundles, and `index.json`
- `.ordeal/traces/compose-*.json`: exact Compose action/fault traces and replay counts
- `tests/test_ordeal_regressions.py`: default regression path
- `tests/ordeal-regressions.json`: portable semantic bindings for CI; commit it with the pytest file
- `ordeal.toml`: explorer configuration

Durable regression loop:
- discover → reproduce → minimize → save regression → verify fix → guard CI
- `supported` means the recorded witness replayed; it is not whole-project proof
- prove the generated test fails before the fix and passes on the same witness after
- `.ordeal/findings/` may remain local; the Python regression and JSON manifest are the durable pair

Discovery:
- `ordeal --help`
- `ordeal <command> --help`
- `from ordeal import catalog; catalog()`
- `docs/guides/compose-runner.md`: plain-English Compose starting point
- `docs/guides/compose-configuration.md`: exact Compose schema and defaults
- `docs/guides/compose-fault-model.md`: exact fault cycles and boundaries
- `docs/guides/compose-traces.md`: trace fields and replay interpretation
- `docs/concepts/durable-regressions.md`: plain-language model
- `docs/guides/durable-regressions.md`: complete operational workflow
- `docs/guides/durable-regressions-ci.md`: provider-neutral CI policy and exit codes
- `docs/reference/durable-regression-schema.md`: exact evidence, binding, and manifest fields
- https://docs.byordeal.com/

Machine surfaces:
- `--json` prints stable agent-facing envelopes
- audit protection rows live at `raw_details.protection_views[]`
- Python consumers can call `MutationResult.test_protection_view()`, `property_strength()`, and `kill_attribution()`
- saved scan bundles include stable `finding_id`

Reliability coverage:
- Add `operation=` and `fault=` to `always`, `sometimes`, `reachable`, or `unreachable` to record an operation × fault × property cell.
- Use contextual `declare(name, type, operation=..., fault=...)` for expected cells; zero observations mean `NOT EXERCISED`, not pass.
- Labels describe a fault the harness really injected; they do not activate faults.
- Run `pytest --chaos` or call `auto_configure()` so the tracker records the matrix.
- Read `report()["reliability_coverage"]` for JSON-safe rows and counts. pytest-xdist workers merge into the controller.
- Keep `PASS`, `NOT EXERCISED`, and `FAIL` separate in explanations and downstream gates.
- Docs: https://docs.byordeal.com/concepts/reliability-coverage/
