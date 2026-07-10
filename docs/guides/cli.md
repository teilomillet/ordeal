---
description: >-
  Full advanced reference for every Ordeal CLI command and option.
---

# CLI

!!! quote "In plain English"
    Most users need one loop: `scan → fix → verify → CI`. This page is the
    complete expert reference for the lower-level commands and tuning controls.

## The path to learn first

```bash
ordeal scan .
ordeal scan . --save
# fix the product code
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci
```

Start with the [Scan Quickstart](scan-quickstart.md). Return to this page only
when the default scan is blocked, the target needs custom setup, or you have a
specialized goal such as mutation scoring, service exploration, or migration.

If you want an AI coding agent to discover the CLI surface inside a repo, run `ordeal skill` or `ordeal init --install-skill` to install the bundled local guide.

## Install

```bash
uv tool install ordeal     # global, `ordeal` on PATH
uvx ordeal scan .          # ephemeral, no install
uv run ordeal scan .       # inside project venv
```

## Complete command reference

All existing commands remain available for compatibility and advanced use.

### `ordeal catalog`

List everything ordeal exposes, grouped by subsystem. This is the quickest way to discover capabilities before choosing a narrower command, and it mirrors the Python `catalog()` API.

```bash
ordeal catalog
ordeal catalog --detail
ordeal catalog --json
```

Use `--detail` when you want the neutral discovery fields for each entry: capability, applicability, expected inputs, outputs, usage patterns, and adjacent learning surfaces. Those fields are derived from the live parser, docstrings, and signatures rather than a hand-maintained command index. Use `--json` for the same capability map in machine-readable form.

| Flag | Default | Description |
|---|---|---|
| `--detail` | off | Show signatures and docstrings for each capability |
| `--json` | off | Emit the full capability map as structured JSON |

### `ordeal check`

Verify one mined property, or the default bug-catching contracts, for a single function. Exit code is `0` when the property holds and `1` when ordeal finds a counterexample.

```bash
ordeal check myapp.scoring.normalize
ordeal check myapp.scoring.normalize -p idempotent
ordeal check myapp.scoring.score -p "output in [0, 1]" -n 500
ordeal check myapp.envs:ComposableEnv.build_env_vars --contract quoted_paths
```

Without `-p`, `check` verifies the standard contracts that catch real bugs quickly: `never None`, `no NaN`, `never empty`, `deterministic`, `idempotent`, and `finite`. For explicit targets, `--contract` lets you run named built-in semantic probes directly, and `--config` reuses any matching `[[objects]]` and `[[contracts]]` entries from `ordeal.toml`.

`check` now also prints ready-to-paste `ordeal.toml` suggestions. Explicit contract checks emit `[[contracts]]` blocks plus any mined `[[objects]]` harness blocks; property-mode checks emit a focused `[[scan]]` block you can keep under versioned config. The JSON envelope mirrors this under `raw_details.config_suggestions`.

| Flag | Default | Description |
|---|---|---|
| `target` | required | Dotted function path such as `myapp.scoring.normalize` |
| `--property`, `-p` | all standard contracts | Check one property by name or substring match |
| `--contract` | `[]` | Repeat to run named built-in semantic contracts such as `quoted_paths` or `cleanup_after_cancellation` |
| `--config` | `./ordeal.toml` when present | Load object factories and configured explicit contracts |
| `--max-examples`, `-n` | `200` | Examples to test |
| `--json` | `false` | Emit the agent envelope instead of text |

### `ordeal scan`

!!! quote "Why start here"
    `scan` is the fastest end-to-end command for turning exploration results into something you can act on, but it is exploratory first. It runs the unified exploration pipeline on one module, then can emit a shareable Markdown report, runnable pytest regressions, and a machine-readable JSON bundle with stable finding IDs.

Explore one module and optionally save reports, regressions, or the full bug bundle:

```bash
ordeal scan myapp.scoring
ordeal scan myapp.scoring --json
ordeal scan ordeal --target mutate --target "audit_*"
ordeal scan myapp.scoring --base-ref origin/main --deepen --time-limit 60
ordeal scan myapp.scoring --report-file findings/scoring.md
ordeal scan myapp.scoring --write-regression
ordeal scan myapp.scoring --save-artifacts
```

Use the [Scan Quickstart](scan-quickstart.md) for the shortest beginner path,
[Object Harnesses and Stateful Replay](scan-object-harnesses.md) for classes,
[Scan Troubleshooting](scan-troubleshooting.md) for blocked/noisy runs, and the
[Scan Evidence Schema](../reference/scan-evidence-schema.md) for JSON fields.
Every scan also emits a source-backed reliability map. Static properties are
explicit hypotheses and remain `NOT EXERCISED` until runtime evidence supports
`PASS` or `FAIL`. `--deepen` runs one cheapest safe planned experiment and
requires an explicit `--time-limit`. Service faults require both
`--allow-service-faults` and `[compose]` configuration. See the
[Evidence Closure Guide](evidence-closure.md).

Use `--save-artifacts` when you want the full handoff package. It writes the
pytest regression plus its portable `tests/ordeal-regressions.json` CI
manifest. The richer local handoff includes a Markdown dossier, JSON bundle,
artifact index, and review-first sidecars: `.ordeal.toml`, support, proofs,
replay notes, and scenarios under `.ordeal/findings/<module>.*`. See
[Bug Bundle](bug-bundle.md) for the artifact layout.

`scan` is evidence-first. In `--mode evidence`, it surfaces replayable crashes, weaker exploratory properties, and expected precondition failures without flattening them into one verdict. `--mode candidate` keeps the same search but ranks only the strongest contract-valid issue candidates at the top. If `[[scan]]` or `[[objects]]` leave fixture completeness too low for the module, `scan` reports that as a block instead of pretending it has real leverage. Add the missing factory, state builder, setup, or collaborator scenario before expecting useful output. If you need stronger validation for a mature codebase, prefer `ordeal audit` for coverage and mutation comparison, and `ordeal mutate` for direct mutation scoring.

`--security-focus` is the opt-in trust-boundary bias for scan. It expands sink inference beyond shell/path/env to import loading, deserialization, filesystem writes, symlink handling, and checkpoint/IPC paths, then adds deterministic low-side-effect probes for pure path/symlink shapers plus small artifact/config mutations for deserialization- and IPC-shaped inputs. Pair it with `shell_injection_check = true` in `[[scan]]` when you want a static input-to-shell-sink oracle to fire before the target is executed. Saved proof bundles keep that context under `impact.critical_sinks`, `impact.trust_boundary_signal`, and `contract_basis.security_focus`.

Every finding now carries a compact [evidence card](finding-evidence.md) in normal text, Markdown, JSON, and saved replay artifacts. It gives the bounded claim, callable-source hash, exact witness and witness hash, immediate replay result, pending same-witness post-fix control, and explicit claim boundaries. `supported` means the same exception type, message, and terminal source location matched every immediate replay; it is not a correctness certificate. Findings without that replay remain `exploratory`, and documented preconditions remain `expected`. When a bound-method finding uses resolvable factory, setup, scenario, state, and teardown hooks, the saved regression reconstructs that discovered harness before replaying the exact witness. Saved regressions include AST and target-import SHA-256 bindings; `ordeal verify` fails closed if the bound test changes, then records whether the post-fix control passed or still reproduces.

Promoted crash findings also carry the deeper proof bundle: contract basis, confidence breakdown, minimal reproduction, failure path, and likely impact. Demoted crashes keep the same structure plus an explicit demotion reason.

See [Scan Finding Rules](scan-finding-rules.md) for the exact heuristics behind helper filtering, source-backed security probes, witness-aligned `critical_sinks`, and replay-required promotion of critical-sink crashes.

Use `--list-targets` when you want to inspect how ordeal sees functions and methods before choosing a target. The listing shows the callable kind, whether it is async or sync, whether a factory is required or configured, and any skip reason if ordeal cannot run it yet. Mined harness hints now carry observed-evidence `score` and `signals`, so the summary and any suggested `[[objects]]` blocks prefer the strongest structurally supported factory or setup hint instead of whichever fixture was discovered first. Use `--target` to limit a module scan to one or more callable selectors. Selectors accept local names, explicit targets, and globs like `mutate`, `Env.*`, or `ordeal:mut*`.

Most of the tuning knobs for `scan` live in `[[scan]]` inside `ordeal.toml`. Use `[fixtures].registries` for project-wide fixture registrations, `fixture_registries` for scan-specific registry imports, `ignore_properties` and `ignore_relations` to suppress noisy laws, and `property_overrides` or `relation_overrides` when one function needs a narrower set of checks. `expected_failures` keeps known preconditions visible without ranking them as issue candidates. For OO code, `[[objects]]` supplies bound-instance factories, `state_factory`, scenarios, and teardown. Scan reconstructs that lifecycle for each witness; `harness = "stateful"` additionally lets `chaos_for` reuse one object across state-machine steps. `[[contracts]]` adds explicit shell/path/env probes.

`scan` now surfaces ready-to-paste `ordeal.toml` suggestions too. The text summary prints a `Suggested ordeal.toml:` block, the JSON envelope includes `raw_details.config_suggestions`, and `--save-artifacts` persists the same review bundle to `.ordeal/findings/<module>.ordeal.toml`. Package-root sampled scans emit a repeatable `[[scan]]` block with the sampled targets, while method-heavy runs can also suggest `[[objects]]` or `[[contracts]]` blocks derived from the observed surface and findings, plus a review scaffold for `tests/ordeal_support.py`.

| Flag | Default | Description |
|---|---|---|
| `target` | required | Module path or explicit callable target such as `myapp.scoring` or `myapp.scoring:Env.build_env_vars` |
| `--target` | `[]` | Repeatable callable selector filter for module scans; accepts exact names, explicit targets, or glob patterns |
| `--seed` | `42` | RNG seed for reproducibility |
| `--max-examples`, `-n` | `50` | Examples per function |
| `--security-focus`, `--no-security-focus` | off | Bias scan toward trust-boundary sinks and deterministic security probes |
| `--workers`, `-w` | `1` | Parallel workers for mutation testing |
| `--time-limit`, `-t` | — | Time budget in seconds |
| `--deepen` | off | Run one safe planned experiment; requires `--time-limit` |
| `--base-ref REF` | — | Prioritize and diff code changed since a Git revision |
| `--allow-service-faults` | off | Permit configured Compose fault experiments during deepening |
| `--json` | off | Print the stable agent-facing JSON envelope |
| `--report-file` | — | Save a Markdown finding report |
| `--write-regression [PATH]` | `tests/test_ordeal_regressions.py` | Save runnable pytest regressions |
| `--save-artifacts` | off | Write the report, JSON bundle, regressions, and update the artifact index |
| `--include-private` | off | Include `_private` functions |
| `--list-targets` | off | List callable targets and metadata, then exit |

### `ordeal init`

Bootstrap ordeal into an existing package. `init` generates starter tests for untested modules, writes `ordeal.toml` when missing, validates the generated tests, and prints a lightweight read-only scan summary for the modules it just bootstrapped.

```bash
ordeal init
ordeal init myapp
ordeal init myapp --dry-run
ordeal init myapp --install-skill
ordeal init myapp --close-gaps
ordeal init myapp --ci
```

`--dry-run` is the safe preview mode: it discovers modules from the filesystem and signatures from AST only, without importing the target package, executing functions, or writing files.

By default, `init` does not install the bundled skill and does not write draft audit gap stub files. Those extra writes are explicit opt-ins.

`init` now also reads `[init]` from `ordeal.toml` when present. That lets you keep bootstrap defaults like `target`, `output_dir`, `close_gaps`, and CI generation in versioned config instead of repeating flags in scripts.

For safety, `audit.save_generated`, `audit.write_gaps_dir`, `init.output_dir`, and `init.gap_output_dir` must stay inside the current workspace root.

| Flag | Default | Description |
|---|---|---|
| `target` | auto-detect | Package path such as `myapp`; omit to detect from the current directory |
| `--config`, `-c` | `ordeal.toml` if present | Load `[init]` defaults from a config file |
| `--output-dir`, `-o` | `tests` | Directory where generated tests are written |
| `--dry-run` | off | Preview generated files without imports or writes |
| `--ci` | off | Generate `.github/workflows/<name>.yml` |
| `--ci-name` | `ordeal` | Workflow filename stem |
| `--install-skill` | off | Also install the bundled AI-agent skill into `.claude/skills/ordeal/` |
| `--close-gaps` | off | Write draft audit stub files for surviving mutation gaps, one file per target |

### `ordeal mutate`

Run mutation testing from the command line. Auto-discovers tests via pytest, runs them with `--chaos` enabled so ChaosTest assertions count toward the mutation score.

```bash
ordeal mutate myapp.scoring.compute                          # single function
ordeal mutate myapp.scoring                                  # whole module
ordeal mutate myapp.scoring --preset thorough --workers 4    # parallel, all operators
ordeal mutate myapp.scoring --threshold 0.8                  # fail if score < 80%
ordeal mutate myapp.scoring --generate-stubs tests/gaps.py   # write test stubs
```

Output always includes a `Score:` line for CI parsing:

```
Mutation score: 15/18 (83%)  [target: myapp.scoring.compute, preset: standard]
  3 test gap(s) — each is a code change your tests fail to catch:
  GAP L42:8 [arithmetic] + -> -  |  return a + b
    Fix: Add an assertion that checks the exact numeric result...
Score: 15/18 (83%)
Threshold: 80% — PASS
```

This score applies to the selected existing tests and mutation configuration.
Inspect every survivor; the aggregate threshold is not a correctness certificate.
Use `result.kill_attribution()` and `result.property_strength()` from Python, or
run `audit` for the combined generated-check protection verdict. See
[Test Protection](test-protection.md) for interpretation.

| Flag | Default | Description |
|---|---|---|
| `targets` | required | Dotted paths (positional, one or more) |
| `--preset` | `standard` | `"essential"`, `"standard"`, or `"thorough"` |
| `--workers` | `0` | Adaptive workers; pass a positive count to override |
| `--threshold` | `0.0` | Minimum score; exit code 1 if below |
| `--generate-stubs` | — | Write test stubs for surviving mutants to this path |
| `--no-filter` | off | Disable equivalence filtering |
| `--equivalence-samples` | `10` | Random inputs for equivalence check |

### `ordeal diff`

Compare one callable or module across two committed Git revisions. Each side
runs from its own detached temporary worktree and subprocess, and the candidate
replays the exact inputs generated by the baseline:

```bash
ordeal diff mypkg.scoring --base-ref origin/main --candidate-ref HEAD
ordeal diff mypkg.scoring --base-ref origin/main --save-artifacts
ordeal diff mypkg.scoring --base-ref origin/main --write-regression
ordeal diff mypkg.Store --base-ref origin/main --sequence-file story.json
ordeal diff  # uses [diff] from ordeal.toml
```

Exit `0` means no divergence was observed in the measured sample, `1` means a
behavior or public-surface divergence was found, and `2` means inconclusive.
`HEAD` excludes uncommitted changes. See [Compare Two Git Revisions](revision-diff.md)
for the first run, [Revision Diff Troubleshooting](revision-diff-troubleshooting.md)
for failures, and the [Revision Diff Schema](../reference/revision-diff-schema.md)
for machine output.
`--write-regression` registers the pinned-base witness for `verify --ci`.
`--sequence-file` switches the target to a zero-argument system factory.
`--replay-attempts N` controls exact immediate replay support.
Read [Divergence Evidence](../concepts/divergence-evidence.md) for the narrative,
then use the [artifact workflow](divergence-evidence.md),
[troubleshooting](divergence-evidence-troubleshooting.md), or
[exact schema](../reference/divergence-evidence-schema.md).

### `ordeal migrate`

Replace one importable module with another without treating "same as before"
as proof of correctness. A perfect parity match can preserve an old bug.

```bash
ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml
ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml \
  --intended-change behavior:normalize
```

The command always audits the base, mines candidate hypotheses, diffs both
modules, classifies changes, saves unexpected divergences, mutates generated
parity checks plus explicit contracts, and scans the candidate independently.
It does not score the candidate project's normal tests. Put domain rules in
matching `[[contracts]]` entries; mined patterns never become correctness rules
automatically. Shared callable signatures are compared, intended behavior needs
callable-scoped protection, and an empty candidate scan cannot pass the gate.

Start with [Safe Module Migrations](../concepts/safe-migrations.md), then use the
[complete migration workflow](migration-workflow.md) for statuses, completion
gates, and artifacts.

### `ordeal mine-pair`

!!! quote "What this unlocks"
    If you have two functions that should be inverses of each other -- like `encode`/`decode` or `serialize`/`parse` -- this command automatically checks whether that's actually true. No test code needed. Just point it at two functions and it tells you if the roundtrip holds.

Discover relational properties between two functions — roundtrip (`g(f(x)) == x`), reverse roundtrip, and commutative composition:

```bash
ordeal mine-pair mymod.encode mymod.decode
ordeal mine-pair json.dumps json.loads -n 500
```

```
mine_pair(encode, decode): 200 examples
  ALWAYS  roundtrip g(f(x)) == x (200/200)
  ALWAYS  roundtrip f(g(x)) == x (200/200)
```

| Flag | Default | Description |
|---|---|---|
| `f` | required | First function (dotted path) |
| `g` | required | Second function (dotted path) |
| `--max-examples`, `-n` | `200` | Examples to sample |

### `ordeal audit`

!!! quote "Why this matters"
    Audit answers the question: "are my tests actually good?" It measures your existing tests, generates ordeal-style replacements, and compares them side by side. Every number is verified, not estimated. If ordeal can match your coverage with less code, you know where your tests have unnecessary complexity, and the remaining mutation gaps tell you where the tests are still weak.

Measure your existing tests vs what ordeal auto-scan achieves — verified numbers, not estimates:

```bash
ordeal audit myapp.scoring --test-dir tests/
ordeal audit myapp.scoring myapp.pipeline -t tests/ --max-examples 50
ordeal audit myapp.scoring --validation-mode deep
```

Output:

```
ordeal audit

  myapp.scoring
    current suite:         33 tests | 343 lines | 98% coverage [verified]
    generated incremental: 12 tests | 130 lines | 100% coverage [verified]
    mined:    deterministic(compute, normalize), output in [0, 1](compute)
    mutation: 14/18 (78%)
    protection: WEAK: 100% line coverage but 4/18 mutation(s) survived
    suggest:
      - L42 in compute(): test when x < 0
      - L67 in normalize(): test that ValueError is raised
```

Every number is `[verified]` (measured and cross-checked for consistency) or
`FAILED: reason`. The protection verdict combines generated/migrated coverage,
mutation survival, and property exercise. It stays `WEAK` when a mutant survives,
even at 100% line coverage. Use `ordeal mutate` when you want the direct verdict
for the selected existing pytest tests rather than audit's resulting checks.

See the [Test Protection Guide](test-protection.md) for the repair workflow,
[Test Protection in CI](test-protection-ci.md) for gates, and the
[Evidence Schema](../reference/test-protection-schema.md) for `--json` consumers.

`audit` is the primary command when you want to judge test quality. It keeps replayable crash evidence separate from exploratory findings, and it returns `weakest_tests` plus `mutation_gap_stubs` so tooling like `init --close-gaps` can write draft follow-up tests without guessing.

When `audit` cannot get enough runnable fixture coverage, it now reports that early as a block instead of flattening everything into generic gap noise. The fix is usually to add or correct the object harness in `[[objects]]` or `[[audit.targets]]`, then raise `audit.min_fixture_completeness` only if you intentionally want a lower bar.

Use `--list-targets` to inspect the callable surface that audit can see, including bound methods and whether their factories are configured. If callable discovery comes back empty for a module but ordeal can still see review-worthy classes, the same command now falls back to class discovery and prints bootstrap scaffolds for `[[audit.targets]]` plus `tests/ordeal_support.py` instead of stopping at `0` targets.

`--validation-mode fast` replays mined inputs against each mutant and is the default because it is much faster. `--validation-mode deep` keeps that replay check and then re-runs `mine()` on each mutant, which is slower but keeps the broader exploratory search.

Audit validation uses one worker by default. `--workers N` uses isolated processes with deterministic per-target seeds and parent-ordered evidence merging. Treat it as an isolation control: ordeal does not claim that more audit workers are faster.

`audit` now reads `[audit]` from `ordeal.toml` too. That means module lists, direct-test gates, fixture-completeness thresholds, validation depth, and gap-writing defaults can live in config and be reused by both humans and agents. Shared `[[objects]]` entries are expanded automatically, and `[[audit.targets]]` lets you override a factory, state factory, teardown, harness, or limit audit to selected methods.

`audit` also emits ready-to-paste config suggestions now. The text report includes a `Suggested ordeal.toml:` block with `[audit]` defaults and any mined `[[objects]]` harness entries for uncovered or exploratory methods. Those harness entries are ranked from observed evidence in nearby tests, support files, and docs, and the same payload is available to agents under `raw_details.config_suggestions`.

The "migrated" column shows what a real ordeal test file looks like: `fuzz()` for crash safety plus explicitly mined properties (bounds, determinism, type checks). It generates the test file a developer would write after adopting ordeal.

Use `--show-generated` to inspect the generated test, or `--save-generated` to save it and use it directly:

```bash
ordeal audit myapp.scoring --show-generated          # print generated test
ordeal audit myapp.scoring --save-generated test_migrated.py  # save to file
```

| Flag | Default | Description |
|---|---|---|
| `modules` | required unless `[audit].modules` is set | Module paths to audit |
| `--config`, `-c` | `ordeal.toml` if present | Load `[audit]` defaults from a config file |
| `--test-dir`, `-t` | `tests` | Directory containing existing tests |
| `--max-examples` | `20` | Hypothesis examples per function |
| `--validation-mode` | `fast` | `fast` replay or `deep` replay + re-mine for mutation validation |
| `[audit].min_fixture_completeness` | `float` | `0.0` | Minimum runnable-target ratio before audit reports a blocked target |
| `--write-gaps` | — | Write draft gap stubs to this path |
| `--include-exploratory-function-gaps` | off | Include indirect-only function gaps in reports and draft stubs |
| `--require-direct-tests` | off | Exit 1 when any function still lacks direct tests |
| `--list-targets` | off | List callable targets and metadata, then exit |
| `--show-generated` | off | Print the generated test file |
| `--save-generated` | — | Save generated test to this path |

### `ordeal mine`

!!! quote "Think of it this way"
    Instead of you guessing what properties a function has, `mine` discovers them automatically. It runs the function hundreds of times with random inputs and tells you what's always true: "output is always a float," "always between 0 and 1," "always deterministic." These discovered properties become your test assertions.

Discover properties of a function or all public functions in a module. Prints what mine() finds — type invariants, algebraic laws, bounds, monotonicity, length relationships — with confidence levels.

```bash
ordeal mine myapp.scoring.compute           # single function
ordeal mine myapp.scoring                   # all public functions
ordeal mine myapp.scoring.compute -n 1000   # more examples = tighter confidence
```

Output:

```
mine(compute): 500 examples
  ALWAYS  output type is float (500/500)
  ALWAYS  deterministic (50/50)
  ALWAYS  output in [0, 1] (500/500)
  ALWAYS  observed range [0.0, 0.9987] (500/500)
  ALWAYS  monotonically non-decreasing (499/499)
    n/a: commutative, associative
```

Use this to understand a function before writing tests. The `ALWAYS` properties are candidates for assertions; the `n/a` list shows what doesn't apply. `result.not_checked` (visible in the Python API) lists what mine() structurally cannot verify — those are the tests you write manually.

| Flag | Default | Description |
|---|---|---|
| `target` | required | Dotted path: `mymod.func` or `mymod` (positional) |
| `--max-examples`, `-n` | `500` | Examples to sample |

### `ordeal seeds`

Inspect the persistent seed corpus that `ordeal explore` builds when it finds failures. Each saved seed is a content-addressed failing trace that ordeal can replay on later runs to confirm the bug still reproduces.

```bash
ordeal seeds
ordeal seeds --dir .ordeal/seeds
ordeal seeds --prune-fixed
```

`--prune-fixed` replays every saved seed first, then removes the ones that no longer reproduce.

| Flag | Default | Description |
|---|---|---|
| `--dir` | `.ordeal/seeds` | Seed corpus directory |
| `--prune-fixed` | off | Remove seeds that replay as fixed |

### `ordeal benchmark`

!!! quote "What you can do with this"
    Before you set `workers = 8` in your config, run `benchmark` to find out if 8 workers actually helps. Some tests hit diminishing returns at 4 workers, others scale to 16. This command measures real throughput and tells you the sweet spot for your specific test and machine.

Measure how parallel exploration scales on your machine and test class. Runs the Explorer at N=1, 2, 4, 8... workers, measures throughput, and fits the Universal Scaling Law (USL):

```bash
ordeal benchmark                          # uses ordeal.toml, first [[tests]] entry
ordeal benchmark -c ci.toml               # custom config
ordeal benchmark --max-workers 16         # test up to 16 workers
ordeal benchmark --time 30                # 30s per trial (default: 10s)
ordeal benchmark --metric edges           # fit on edges/sec instead of runs/sec
ordeal benchmark --perf-contract ordeal.perf.toml --check
ordeal benchmark --bug-manifest benchmarks/bug-benchmark.example.toml --benchmark-tier public
ordeal benchmark --mutate myapp.scoring.compute --repeat 5 --workers 2
```

```
Scaling Analysis (Universal Scaling Law)
  sigma (contention):  0.030695
  kappa (coherence):   0.000000
  Regime:              amdahl
  Fit status:          CONCLUSIVE
  Approx. sigma 95% CI: [0.000000, 0.789053]
  Approx. kappa 95% CI: [0.000000, 0.101648]
  R^2 (throughput):    0.9909
  RMSE (throughput):   0.1972x
  Max rel. residual:   7.2%

  Observed vs fitted scaling:
    N=  8: observed 6.81x (85.1% efficient), fitted 6.59x (82.3% efficient)
```

The fitter enforces non-negative coefficients and refits on a constraint
boundary. The report includes residuals, throughput R², approximate 95%
coefficient intervals, and observed efficiency beside fitted efficiency. A fit
with fewer than three informative worker counts, R² below 0.90, or a relative
residual above 20% is `INCONCLUSIVE`; do not use its optimal-worker projection.

For checked-in benchmark contracts, `--perf-contract` enforces both latency and audit-quality drift in CI. Add `--output-json perf.json` when an agent or CI job needs machine-readable results:

```toml
[[cases]]
name = "audit_demo_fast_vs_deep"
kind = "audit_compare"
module = "ordeal.demo"
validation_mode = "fast"
compare_validation_mode = "deep"
max_score_gap = 0.10
min_score = 0.80
```

That case fails if fast validation falls more than 10 percentage points behind
deep validation or if its absolute mutation score drops below 80%.
Use `--output-json perf.json` when you want a trendable artifact for CI or nightly runs.

The JSON artifact includes the contract path, per-case pass/fail state, timing medians, and the exact score-gap data for `audit_compare` cases.

For bug-discovery benchmarking, `--bug-manifest` scores the real `ordeal scan --json` workflow on curated public or private bug cases. Use a public tier for comparability and a private tier for optimization without benchmark saturation:

```bash
ordeal benchmark --bug-manifest benchmarks/bug-benchmark.example.toml --benchmark-tier public
ordeal benchmark --bug-manifest benchmarks/bug-benchmark.example.toml --benchmark-tier private --check
ordeal benchmark --verify-evidence benchmarks/evidence/httpie-3.toml --online-sources
```

Bug-manifest JSON artifacts include positive bugs, fixed negative controls,
precision/recall/specificity, Wilson bounds, provenance, integrity digests, and
raw scans. See [Bug Benchmarks](benchmark-manifests.md) and
[Bug Evidence Records](bug-evidence-records.md) for the deliberately limited
claims.
Each case must also declare its oracle source, selection reason, saturation risk, and whether it is allowed for optimization; the runner rejects public cases that are marked as tuning targets.
Use `requires_python` for the runner constraint and `oracle_python_version` for
the historical upstream environment. Incompatible runners are `blocked`
instead of being scored as misses.

You can also benchmark mutation latency instead of scaling by passing one or more `--mutate` targets. That mode runs fresh subprocess trials and reports median wall time plus per-phase timings:

```bash
ordeal benchmark --mutate tests._mutation_bench_target.tiny_add --repeat 5
ordeal benchmark --mutate myapp.scoring.compute --workers 4 --preset essential
ordeal benchmark --mutate myapp.scoring.compute --test-filter test_compute
```

| Flag | Default | Description |
|---|---|---|
| `--config`, `-c` | `ordeal.toml` | Config file |
| `--max-workers` | CPU count | Maximum workers to test |
| `--time` | `10` | Seconds per trial |
| `--metric` | `runs` | `"runs"` (runs/sec) or `"edges"` (edges/sec) |
| `--perf-contract` | — | Run a checked-in perf/quality contract instead of scaling analysis |
| `--check` | off | Exit 1 on contract failure; certified manifests fail closed on thresholds or provenance |
| `--output-json` | — | Save contract, benchmark, or verification results as JSON |
| `--json` | off | Print contract, benchmark, or verification JSON to stdout |
| `--tier` | all | Filter perf-contract cases by tier (`pr` or `nightly`) |
| `--bug-manifest` | — | Run a curated bug benchmark manifest against `ordeal scan --json` |
| `--verify-certificate` | — | Verify a bug-benchmark evidence certificate |
| `--verify-evidence` | — | Verify one executable, source-backed bug record |
| `--online-sources` | off | Fetch and hash pinned authoritative evidence URLs |
| `--certificate-manifest` | — | Require exact manifest bytes during certificate verification |
| `--benchmark-tier` | all | Filter bug-manifest cases by their `tier` label |
| `--bugsinpy-root` | — | Root of a local BugsInPy checkout used for original-corpus cases |
| `--checkout-root` | `.ordeal/bug-benchmark` | Where temporary BugsInPy workspaces are materialized |
| `--mutate` | — | Benchmark mutation latency for this target (repeatable) |
| `--repeat` | `5` | Fresh subprocess runs per mutation target |
| `--workers` | `1` | Worker count for mutation benchmarks |
| `--preset` | `standard` | Mutation preset for mutation benchmarks |
| `--test-filter` | — | Pytest `-k` filter for mutation benchmarks |
| `--no-filter-equivalent` | off | Disable equivalence filtering during mutation benchmarks |

### `ordeal explore`

!!! quote "The key insight"
    This is the core of ordeal. It reads your config, loads your ChaosTest classes, and systematically explores what happens when things go wrong -- different rule orderings, different fault combinations, different timings. It's like having a tireless QA engineer who tries thousands of scenarios while you write code.

Your main command for deep exploration. Reads `ordeal.toml`, loads each ChaosTest class, and runs coverage-guided exploration with fault injection, energy scheduling, and swarm mode.

Use for: pre-commit validation, pre-release exploration runs, CI pipelines, and finding deep bugs that unit tests miss.

```bash
ordeal explore                          # reads ordeal.toml
ordeal explore -c ci.toml              # custom config
ordeal explore -v                       # live progress
ordeal explore --max-time 300          # override time
ordeal explore --seed 99               # override seed
ordeal explore --runner compose        # long-lived Docker Compose services
ordeal explore --runner compose --replay-attempts 10
ordeal explore --runner compose --save-artifacts  # durable bound trace + manifest
ordeal explore --runner compose --save-artifacts --json  # complete run evidence
ordeal explore --no-shrink             # skip failure minimization
ordeal explore -w 4                    # 4 parallel workers
ordeal explore --generate-tests tests/test_generated.py  # turn traces into pytest tests
```

The `--workers` / `-w` flag runs exploration across multiple processes. Each worker gets a unique seed for independent state-space exploration. Results are aggregated: runs/steps are summed, edges are unioned for true unique count. Use `--workers $(nproc)` for full CPU utilization.

The `compose` runner reads `[compose]` and `[[compose.requests]]`, keeps one service
topology alive, preserves captured JSON state, injects worker and response faults,
and saves the exact action sequence. See [Compose Services](compose-runner.md).

| Flag | Python runner | Compose runner |
|---|---|---|
| `--runner` | `python` (default) | set `compose` |
| `--config`, `-c` | `[explorer]` and `[[tests]]` | `[compose]` and `[[compose.requests]]` |
| `--seed` | exploration seed | request, service, and fault selection seed |
| `--max-time` | Python exploration budget | service exploration budget |
| `--replay-attempts` | not used | override immediate failure attempts |
| `--save-artifacts` | not used | persist complete run evidence and promote replay-backed failures |
| `--json` | not used | complete `ordeal.compose-run/v1` evidence |
| `--workers`, `-w` | parallel process count | must be omitted or `1` |
| `--generate-tests` | supported | rejected |
| `--resume` / `--save-state` | trusted pickle state | rejected |

For the service runner's full command workflow, start with the
[Compose Quickstart](compose-quickstart.md). All configuration fields are in
[Compose Configuration](compose-configuration.md); the full promotion path is
[Compose Evidence and Durable Regressions](compose-evidence-loop.md).

### `ordeal replay`

!!! quote "How to explore this"
    When `explore` finds a bug, it saves a trace -- the exact sequence of steps that triggered the failure. `replay` re-runs those steps so you can see the bug happen again. Use `--shrink` to strip the trace down to the minimum steps needed, which makes the bug much easier to understand.

Reproduce a failure from a saved trace. The trace file contains the exact sequence of rules and fault toggles that triggered the failure, so replaying it re-executes the same steps.

Use for: triaging a CI failure, sharing a reproducible bug with a colleague, verifying that a fix actually resolves the issue.

```bash
ordeal replay .ordeal/traces/fail-run-42.json          # reproduce
ordeal replay --shrink trace.json                       # minimize
ordeal replay --shrink trace.json -o minimal.json      # save minimized
ordeal replay compose-trace.json --attempts 10          # probabilistic service replay
```

The `--shrink` flag runs delta-debugging to remove unnecessary steps from the trace. Use it when: the trace is too long to understand, or you want the minimal sequence of operations that reproduces the failure. The shrunk trace is often 5-10x shorter than the original.

Compose traces are not shrunk. Ordeal replays their exact actions `N` times and
reports `attempted N / reproduced M`, because real service timing is not perfectly
deterministic.

| Replay flag | Python trace | Compose trace |
|---|---|---|
| `--shrink` | minimize steps | rejected |
| `--ablate` | test fault necessity | rejected |
| `--output`, `-o` | save shrunk trace | rejected |
| `--attempts N` | not used | repeat exact actions N times |
| `--json` | agent envelope | Compose trace/replay JSON summary |

See [Compose Traces](compose-traces.md) for failure kinds, exact signature
matching, exit codes, confidentiality, and the nondeterminism boundary.

### `ordeal verify`

Re-run one saved regression and record the post-fix result, or guard every
bound regression without modifying evidence history:

```bash
ordeal verify fnd_dcb0fc0808d3 --allow-unsafe-artifacts
ordeal verify --ci
```

Use the finding form after fixing a bug found by `ordeal scan --save-artifacts`.
It checks the saved test/import binding, runs only that regression, and records
the same-witness control. `--ci` is read-only and provider-neutral: it resolves
repository-relative artifacts in the current checkout, refuses paths outside
that checkout, and fails if any binding in `tests/ordeal-regressions.json`
changed or its regression fails. Commit that manifest with
`tests/test_ordeal_regressions.py`; `.ordeal/findings/` can remain local.

See the [plain-language model](../concepts/durable-regressions.md), the
[complete workflow](durable-regressions.md), or the
[machine contract](../reference/durable-regression-schema.md).

| Flag | Default | Description |
|---|---|---|
| `finding_id` | required unless `--ci` | Stable finding ID from the JSON bundle or index |
| `--index` | `.ordeal/findings/index.json` | Artifact index to read and update |
| `--manifest` | `tests/ordeal-regressions.json` | Portable manifest read by `--ci` |
| `--allow-unsafe-artifacts` | off | Trust saved repo tests for one post-fix verification |
| `--ci` | off | Read-only guard over every saved regression and binding |

### Agent-facing JSON

`scan`, `mine`, `audit`, `mutate`, `replay`, and `benchmark --perf-contract` can emit machine-readable JSON for tooling and coding agents. Use `--json` to print to stdout.

```bash
ordeal scan myapp.scoring --json
ordeal mutate myapp.scoring.compute --json
ordeal audit myapp.scoring --json
```

The payload is a stable envelope with top-level keys like `schema_version`, `tool`, `target`, `status`, `summary`, `recommended_action`, `findings`, `artifacts`, and `raw_details`. `raw_details.config_suggestions` now carries ready-to-paste `ordeal.toml` snippets for `scan`, `audit`, and `check`. For `scan`, `--save-artifacts` complements this with a persistent JSON bug bundle under `.ordeal/findings/`.

### `ordeal skill`

Install ordeal's bundled `SKILL.md` into `.claude/skills/ordeal/` so an AI coding agent has the local capability map in-repo.

```bash
ordeal skill
ordeal skill --dry-run
```

Run this directly when you want to refresh the skill file without re-running `ordeal init`. If you want `init` to install the same skill during bootstrap, pass `--install-skill`.

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | Show the path that would be written without writing it |

## Workflows

!!! quote "In plain English"
    These workflows show how ordeal fits into your daily development cycle. The pattern is simple: explore fast while coding, explore thoroughly in CI, and when something fails, replay and shrink the trace until you understand the bug.

### Local development

Quick exploration with live progress. Run this before committing to catch obvious issues:

```bash
ordeal explore -v --max-time 30
```

The `-v` flag prints a progress line showing runs, steps, edges discovered, and failures found. Thirty seconds is enough to catch most shallow bugs.

### CI pipeline

Longer exploration with a dedicated config, JSON report, and a nonzero exit code on failure:

```bash
ordeal explore -c ci.toml
```

Where `ci.toml` might set `max_time = 120`, `report.format = "json"`, and `report.output = "ordeal-report.json"`. The exit code is 1 if any failure is found, so your CI script can gate on it directly.

### Bug triage

When a CI run or colleague reports a failure trace:

```bash
ordeal replay trace.json                          # confirm it reproduces
ordeal replay --shrink trace.json -o minimal.json # minimize it
```

The shrunk trace gives you the shortest sequence of operations that triggers the bug. Read through the steps: which rules ran, which faults were active, and where the exception occurred.

### Reproducibility

Fix the seed for deterministic exploration. The same seed produces the same sequence of rule interleavings and fault schedules:

```bash
ordeal explore --seed 42
```

Useful for: bisecting changes (did this commit introduce the failure?), comparing exploration runs across branches, and ensuring consistent CI behavior.

## pytest integration

!!! quote "Think of it this way"
    You don't have to choose between pytest and the ordeal CLI -- they work together. pytest is great when you want chaos testing mixed into your regular test suite. The CLI is great for standalone exploration runs. Most teams use both: pytest with `--chaos` in CI, and `ordeal explore` for deeper pre-release validation.

ordeal also works as a pytest plugin (auto-registered when ordeal is installed). No configuration needed -- pytest picks it up automatically via the `pytest11` entry point.

### How `--chaos` works

```bash
pytest --chaos                    # enable chaos mode
pytest --chaos --chaos-seed 42    # reproducible seed
pytest --chaos --buggify-prob 0.2 # higher fault probability
```

When you pass `--chaos`, three things happen:

1. **PropertyTracker activates**: all `always()`, `sometimes()`, `reachable()`, and `unreachable()` calls start recording hits and results instead of being no-ops.
2. **buggify() activates**: every `buggify()` call in your code has a chance of returning True (default 10%, controlled by `--buggify-prob`).
3. **Chaos-only tests run**: tests marked with `@pytest.mark.chaos` are collected instead of skipped.

Without `--chaos`, `buggify()` returns False and chaos-marked tests are
skipped. `always()` and `unreachable()` still raise immediately on violations;
`sometimes()` and `reachable()` do not accumulate deferred evidence. Use
`auto_configure()` when another runner needs the same tracking without the CLI
flag.

### `@pytest.mark.chaos`

Mark tests that should only run under chaos mode. These are skipped without the `--chaos` flag, so your normal CI runs are not affected:

```python
import pytest

@pytest.mark.chaos
def test_under_chaos():
    ...
```

This is useful for tests that are slow (because they explore fault interleavings), flaky by design (because faults cause nondeterminism), or only meaningful under fault injection.

### The property report

Ordeal prints a property report at the end of the test run whenever there are tracked results — with or without `--chaos`. It shows every tracked property, its type, hit count, and pass/fail status:

```
--- Ordeal Property Results ---
  PASS  cache hit (sometimes: 47 hits)
  PASS  no data loss (always: 312 hits)
  FAIL  stale read (sometimes: never true in 200 hits)

  1/3 properties FAILED
```

`always` properties pass if they held every time they were evaluated. `sometimes` properties pass if they held at least once. `reachable` properties pass if the code path was reached. `unreachable` properties pass if it was never reached.

### The reliability coverage matrix

Add both `operation` and `fault` to any property assertion to record what was
checked under a specific failure:

```python
always(
    charge_count == 1,
    "no_duplicate_charge",
    operation="create_order",
    fault="timeout",
)
```

Use contextual `declare()` for cells the suite intends to exercise. A declared
cell with zero observations remains visible as `NOT EXERCISED`:

```text
--- Ordeal Reliability Coverage ---
  operation × fault × property
  create_order × timeout × no_duplicate_charge     PASS
  create_order × worker_restart × eventual_commit  NOT EXERCISED
  refund × stale_response × balance_conserved      FAIL

  1 PASS, 1 NOT EXERCISED, 1 FAIL
```

The labels do not inject faults. They describe a scenario the test harness
actually arranged. Under pytest-xdist, workers send raw counters to the
controller and Ordeal prints one merged matrix.

See the [plain-English explanation](../concepts/reliability-coverage.md),
[test-author guide](reliability-coverage.md), and
[CI/platform guide](reliability-coverage-ci.md).

### `chaos_enabled` fixture

For tests that need chaos in a specific scope without requiring the global `--chaos` flag:

```python
def test_something(chaos_enabled):
    # buggify() is active, PropertyTracker is recording
    result = my_function()
    assert result is not None
```

The fixture activates buggify and the PropertyTracker for the duration of the test, then restores the previous state.

### Pytest patterns

**Pattern 1: Separate chaos tests from unit tests.** Keep chaos tests in their own directory so you can run them independently:

```
tests/
├── unit/              # fast, deterministic — always run
│   └── test_scoring.py
├── chaos/             # slower, exploratory — run with --chaos
│   └── test_scoring_chaos.py
└── conftest.py
```

```bash
pytest tests/unit/                          # fast CI gate
pytest tests/chaos/ --chaos --chaos-seed 0  # thorough validation
```

**Pattern 2: Use `chaos_enabled` for targeted chaos in unit tests.** You don't need `--chaos` for everything. Use the fixture when a specific test needs fault injection:

```python
def test_retry_logic(chaos_enabled):
    """This test specifically checks retry behavior under buggify."""
    from ordeal.buggify import buggify
    # buggify() is now active — it will sometimes return True
    result = service_with_retries.call()
    assert result is not None  # should succeed despite faults
```

**Pattern 3: Combine `@pytest.mark.chaos` with `ChaosTest.TestCase`.** ChaosTest classes work with or without `--chaos`, but marking them ensures they're skipped in fast CI runs:

```python
import pytest
from ordeal import ChaosTest, rule, always

@pytest.mark.chaos
class ScoreServiceChaos(ChaosTest):
    faults = [...]
    @rule()
    def score(self): ...

TestScoreServiceChaos = ScoreServiceChaos.TestCase
```

**Pattern 4: Auto-scan via ordeal.toml.** When you add `[[scan]]` entries to `ordeal.toml`, pytest auto-discovers and runs them. No test files needed:

```toml
# ordeal.toml
[[scan]]
module = "myapp.scoring"
max_examples = 100
```

```bash
pytest ordeal.toml --chaos  # auto-scans myapp.scoring
```

Each public function in the module becomes a test item. Functions without type hints are skipped unless fixtures are provided in the TOML.

**Pattern 5: Different buggify probabilities for different environments.**

```bash
pytest --chaos --buggify-prob 0.05   # gentle: 5% fault rate (local dev)
pytest --chaos --buggify-prob 0.1    # moderate: 10% (default, CI)
pytest --chaos --buggify-prob 0.3    # aggressive: 30% (pre-release stress)
```

Higher probability = more faults per run = finds more bugs but also more noise. Start gentle, increase as your error handling matures.

## Exit codes

`ordeal scan` returns **0** when no scan findings are counted, **1** when it
reports findings or a blocked scan, and **2** for invalid command usage.

`ordeal explore` returns **0** on success (no failures found) and **1** if any failure is found or if there is a configuration error. Use this directly in CI scripts:

```bash
ordeal explore -c ci.toml || exit 1
```

`ordeal replay` returns **0** if the failure did not reproduce (which can happen if the code has changed) and **1** if the failure reproduced.
