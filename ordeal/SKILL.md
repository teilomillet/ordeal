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
- `ordeal diff <target> --base-ref origin/main --candidate-ref HEAD`
- `ordeal migrate <base-module> <candidate-module> -c ordeal.toml`

Read-first CLI:
- `ordeal scan <target>`: unified bug discovery; `--save-artifacts` writes report, JSON bundle, regressions, and index entries
- `ordeal mine <target>`: discover suspicious properties
- `ordeal audit <target>`: measure gaps in existing tests
- `ordeal verify <finding-id> --allow-unsafe-artifacts`: re-run one bound regression after a fix and record the result
- `ordeal verify --ci`: read-only guard for every regression in `tests/ordeal-regressions.json`
- `ordeal explore -c ordeal.toml`: deeper coverage-guided exploration
- `ordeal explore --runner compose`: long-lived services, reliability coverage, optional workload mutations, and exact traces with repeated replay counts
- `ordeal explore --runner compose --save-artifacts`: promote a replay-backed failure into a bound portable trace and the shared regression manifest
- `ordeal diff <target>`: compare committed revisions in separate worktrees/subprocesses; `--save-artifacts` writes JSON and Markdown evidence
- `diff(Old, New, sequence=[...])`: compare stateful interfaces, outcomes, state, faults, recovery, and optional performance through the same Python API
- `ordeal migrate <base> <candidate>`: run the ordered audit/mine/diff/classify/regress/mutate/scan migration gate

Migration learning path:
- `docs/concepts/safe-migrations.md`: layman explanation of why parity can preserve an old bug
- `docs/guides/migration-workflow.md`: complete command, statuses, decisions, gates, and artifacts
- `docs/reference/api.md#migration-workflow`: exact Python contract

Migration interpretation:
- mined patterns are hypotheses, not business truth
- differential parity reports what stayed the same; it cannot prove that behavior correct
- the strongest verdict requires explicit invariants, all measured mutants killed, and a passing candidate-only scan

Differential testing learning path:
- `docs/concepts/differential-testing.md`: layman-first mental model and claim boundaries
- `docs/guides/differential-quickstart.md`: first copy-paste function comparison
- `docs/guides/differential-state-and-effects.md`: mutations, bound receivers, and selected external effects
- `docs/guides/differential-evidence.md`: four statuses, one minimized witness, exact replay, and JSON evidence
- `docs/concepts/divergence-evidence.md`: source-bound divergence evidence explained from story to artifact
- `docs/guides/divergence-evidence.md`: artifact workflow from comparison to regression
- `docs/guides/divergence-evidence-troubleshooting.md`: missing or unstable artifacts
- `docs/reference/divergence-evidence-schema.md`: exact machine-readable card fields
- `docs/guides/revision-diff.md`: committed Git revisions in isolated worktrees
- `docs/guides/revision-diff-troubleshooting.md`: ref, import, fixture, replay, artifact, and trust-boundary failures
- `docs/reference/revision-diff-schema.md`: exact revision result and embedded divergence-evidence fields
- `docs/concepts/system-differential.md`: layman model for one shared system story
- `docs/guides/system-differential.md`: first copyable stateful comparison
- `docs/guides/system-differential-recipes.md`: state, effects, APIs, faults, and budgets
- `docs/guides/system-differential-troubleshooting.md`: surprising results and fixes
- `docs/reference/system-differential.md`: exact events, fields, and boundaries

Do not collapse the three entry points: `diff(old, new)` compares functions,
`diff(Old, New, sequence=[...])` compares stateful systems, and
`ordeal diff TARGET` compares committed Git revisions.

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
- `ordeal diff <target> --save-artifacts`: writes `.ordeal/diff/<target>.json` and `.md`
- `ordeal migrate <base> <candidate>`: writes `.ordeal/migrations/<pair>.json` and replayable pytest regressions for unexpected changes

Artifacts:
- `.ordeal/findings/`: Markdown reports, JSON bundles, and `index.json`
- `.ordeal/traces/compose-*.json`: exact Compose action/fault traces and replay counts
- `tests/ordeal-compose-regressions/`: committed Compose post-fix controls bound by canonical trace hashes
- `.ordeal/diff/`: revision diff JSON and Markdown handoffs
- `.ordeal/migrations/`: module-migration evidence and generated parity-regression bindings
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
- `docs/concepts/service-evidence-loop.md`: layman model for the complete service evidence loop
- `docs/guides/compose-configuration.md`: exact Compose schema and defaults
- `docs/guides/compose-fault-model.md`: exact fault cycles and boundaries
- `docs/guides/compose-traces.md`: trace fields and replay interpretation
- `docs/guides/compose-evidence-loop.md`: copyable failure-to-CI workflow and checked-in acceptance example
- `docs/concepts/durable-regressions.md`: plain-language regression model
- `docs/guides/durable-regressions.md`: complete regression workflow
- `docs/guides/durable-regressions-ci.md`: provider-neutral CI policy and exit codes
- `docs/reference/durable-regression-schema.md`: exact evidence, binding, and manifest fields
- `docs/concepts/safe-migrations.md`: layman model for safe module replacement
- `docs/guides/migration-workflow.md`: operational migration gate
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
