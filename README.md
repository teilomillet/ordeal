# ordeal

[![CI](https://github.com/teilomillet/ordeal/actions/workflows/ci.yml/badge.svg)](https://github.com/teilomillet/ordeal/actions/workflows/ci.yml)
[![Docs](https://github.com/teilomillet/ordeal/actions/workflows/docs.yml/badge.svg)](https://docs.byordeal.com/)
[![PyPI](https://img.shields.io/pypi/v/ordeal)](https://pypi.org/project/ordeal/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/ordeal)](https://pypi.org/project/ordeal/)
[![License](https://img.shields.io/github/license/teilomillet/ordeal)](LICENSE)

**Your tests pass. Your code still breaks.**

Ordeal finds what you missed — edge cases, untested code paths, bugs that only show up in production. No test code to write. Just point and run.

Open a terminal and paste this ([uvx](https://docs.astral.sh/uv/guides/tools/) runs Python tools without installing them):

```bash
uvx ordeal mine ordeal.demo
```

```
mine(score): 500 examples
  ALWAYS  output in [0, 1] (500/500)         ← always returns a value between 0 and 1
  ALWAYS  monotonically non-decreasing        ← bigger input = bigger output, always

mine(normalize): 500 examples
     97%  idempotent (29/30)                  ← normalizing twice should give the same result
                                                 ...but ordeal found a case where it doesn't
```

Now point it at your code. If you have `myapp/scoring.py`, the module path is `myapp.scoring`:

```bash
uvx ordeal mine myapp.scoring       # what do my functions actually do?
uvx ordeal audit myapp.scoring      # what are my tests missing?
```

Or let your AI assistant do it — open Claude Code, Cursor, or any coding assistant and paste:

> "Run `uvx ordeal mine` and `uvx ordeal audit` on my main modules. Explain what it finds and fix the issues."

ordeal ships with an [AGENTS.md](https://github.com/teilomillet/ordeal/blob/main/AGENTS.md) — your AI reads it automatically and knows how to use every command.

```
pip install ordeal                        # or: uv tool install ordeal
```

## 30-second example

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical

class MyServiceChaos(ChaosTest):
    faults = [
        timing.timeout("myapp.api.call"),         # API times out
        numerical.nan_injection("myapp.predict"),  # model returns NaN
    ]

    @rule()
    def call_service(self):
        result = self.service.process("input")
        always(result is not None, "process never returns None")

    @invariant()
    def no_corruption(self):
        for item in self.service.results:
            always(not math.isnan(item), "no NaN in output")

TestMyServiceChaos = MyServiceChaos.TestCase
```

```bash
pytest --chaos                    # explore fault interleavings
pytest --chaos --chaos-seed 42    # reproduce exactly
ordeal explore                    # coverage-guided, reads ordeal.toml
ordeal explore --runner compose   # long-lived Docker Compose services
```

You declare what can go wrong (faults), what your system does (rules), and what must stay true (assertions). Ordeal explores the combinations.

## Move from functions to real services

Already have a Docker Compose application? Ordeal can keep the topology alive,
send repeated HTTP requests, kill or restart workers, delay or corrupt responses,
carry IDs and tokens between operations, and save the exact action sequence.

```toml
[compose]
base_url = "http://127.0.0.1:8080"
health_path = "/health"
services = ["api", "worker"]

[[compose.requests]]
name = "list-items"
path = "/items"
```

```bash
ordeal explore --runner compose
ordeal explore --runner compose --save-artifacts  # bound trace + durable manifest
```

Real service timing is not perfectly deterministic. Ordeal says so directly:
the trace is exact, while replay is reported as `attempted N / reproduced M`.
The complete service evidence loop is:
`explore → coverage → exact replay → bounded finding → committed regression → verify --ci → test the test`.

New to those terms? Start with
[From “it broke once” to “it cannot come back”](https://docs.byordeal.com/concepts/service-evidence-loop/).
Then follow the copyable
[Compose evidence loop](https://docs.byordeal.com/guides/compose-evidence-loop/)
or use the [plain-English service overview](https://docs.byordeal.com/guides/compose-runner/)
to choose a quickstart, schema, fault model, trace reference, CI guide, or fix.

Ordeal itself runs a real Docker Compose kill-and-recovery gate before publishing.
See [Compose CI and operations](https://docs.byordeal.com/guides/compose-operations/)
for the exact sequence, what a green job proves, and what remains outside its scope.

## Know what reliability behavior was actually tested

Line coverage can tell you that order code ran. It cannot tell you whether an
API timeout happened while an order was created, or whether duplicate charging
was checked afterward. Add `operation` and `fault` to existing assertions:

```python
from ordeal import always, declare

declare(
    "eventual_commit",
    "sometimes",
    operation="create_order",
    fault="worker_restart",
)

always(
    charge_count == 1,
    "no_duplicate_charge",
    operation="create_order",
    fault="timeout",
)
```

The end of `pytest --chaos` shows the evidence directly:

```text
operation × fault × property
create_order × timeout × no_duplicate_charge     PASS
create_order × worker_restart × eventual_commit  NOT EXERCISED
refund × stale_response × balance_conserved      FAIL
```

`PASS` means the cell was observed and held. `NOT EXERCISED` exposes an
expected test that never happened. `FAIL` means Ordeal observed a violation.
The same rows are available as JSON from `report()`, including under
pytest-xdist. Labels describe a fault your test really injected; they do not
inject it themselves.

Start with [Reliability Coverage](https://docs.byordeal.com/concepts/reliability-coverage/),
then use the [practical guide](https://docs.byordeal.com/guides/reliability-coverage/)
and [CI/platform guide](https://docs.byordeal.com/guides/reliability-coverage-ci/).

## Why ordeal

Testing catches bugs you can imagine. The dangerous bugs are the ones you can't — a timeout *during* a retry, a NaN *inside* a recovery path, a permission error *after* the cache warmed up. These come from combinations, and the space of combinations is too large to explore by hand.

Ordeal automates this. It brings ideas from the most rigorous engineering cultures in the world to Python:

| What | Idea | From |
|---|---|---|
| Stateful chaos testing with nemesis | An adversary toggles faults while Hypothesis explores interleavings | [Jepsen](https://jepsen.io) + [Hypothesis](https://hypothesis.works) |
| Coverage-guided exploration | Save checkpoints at new code paths, branch from productive states | [Antithesis](https://antithesis.com) |
| Property assertions | `always`, `sometimes`, `reachable`, `unreachable` — accumulate evidence across runs | [Antithesis](https://antithesis.com/docs/properties_assertions/) |
| Inline fault injection | `buggify()` — no-op in production, probabilistic fault in testing | [FoundationDB](https://apple.github.io/foundationdb/testing.html) |
| Boundary-biased generation | Test at 0, -1, empty, max-length — where bugs actually cluster | [Jane Street QuickCheck](https://blog.janestreet.com/quickcheck-for-core/) |
| Mutation testing | Flip `+` to `-`, `<` to `<=` — verify your tests actually catch real bugs | [Meta ACH](https://engineering.fb.com) |
| Differential testing | Compare functions or replay one whole operation-and-fault story across two systems | Refactor validation |
| Property mining | Discover invariants from execution traces — type, bounds, monotonicity | Specification mining |
| Metamorphic testing | Check output *relationships* across transformed inputs | [Metamorphic relations](https://en.wikipedia.org/wiki/Metamorphic_testing) |

**Read the full [philosophy](https://docs.byordeal.com/philosophy) to understand why this matters.**

## Evidence you can act on

`ordeal scan myapp.scoring` gives each finding a bounded evidence card:

- the exact claim justified by this finding
- a SHA-256 binding to the inspected callable source
- the exact input witness and its canonical hash
- immediate replay counts, matching the same exception type, message, and source seam
- a pending same-witness regression to run after the fix
- explicit limits: no root-cause claim, no claim about untested behavior

A `supported` finding reproduced exactly in the recorded attempts. An
`exploratory` finding is still a lead, and an `expected` precondition is not a
defect. None of these statuses certifies the whole project. The pass or finding
applies only to the inputs, states, faults, properties, and runtime actually
exercised. See [Finding Evidence](https://docs.byordeal.com/guides/finding-evidence/).
For a jargon-free explanation of why this becomes a permanent test, read
[Fix a Bug Once](https://docs.byordeal.com/concepts/durable-regressions/).

Choose the scan documentation at your depth:

| Reader | Start here |
|---|---|
| First-time or non-specialist | [Scan Quickstart](https://docs.byordeal.com/guides/scan-quickstart/) |
| Testing application/stateful objects | [Object Harnesses and Stateful Replay](https://docs.byordeal.com/guides/scan-object-harnesses/) |
| Debugging a blocked or noisy scan | [Scan Troubleshooting](https://docs.byordeal.com/guides/scan-troubleshooting/) |
| Building tools or reviewing proof JSON | [Scan Evidence Schema](https://docs.byordeal.com/reference/scan-evidence-schema/) |

The [durable regression workflow](https://docs.byordeal.com/guides/durable-regressions/)
is one path, not a report handoff:

```bash
ordeal scan myapp.scoring --save-artifacts  # discover, replay, minimize, save
# fix the code
ordeal verify <finding-id> --allow-unsafe-artifacts
ordeal verify --ci                          # provider-neutral CI guard
```

Commit `tests/test_ordeal_regressions.py` and
`tests/ordeal-regressions.json`; the richer `.ordeal/findings/` dossier can
remain local.

Use [Durable Regressions in CI](https://docs.byordeal.com/guides/durable-regressions-ci/)
for rollout and exit codes, the [FAQ](https://docs.byordeal.com/guides/durable-regressions-faq/)
for common decisions, and the [schema reference](https://docs.byordeal.com/reference/durable-regression-schema/)
for integrations and proof review.

## Install

```bash
# From PyPI
pip install ordeal

# With extras
pip install ordeal[atheris]    # coverage-guided fuzzing via Atheris
pip install ordeal[all]        # everything

# As a CLI tool
uv tool install ordeal         # global install
uvx ordeal explore             # ephemeral, no install

# Development
git clone https://github.com/teilomillet/ordeal
cd ordeal && uv sync --locked --extra dev
uv run pytest                  # run the full test suite
```

When dependency metadata changes, run `uv lock` and commit `uv.lock` in the
same change. Normal development should use `uv sync --locked` so lock drift
fails early instead of rewriting the lockfile locally.

## What's in the box

### Stateful chaos testing

`ChaosTest` extends Hypothesis's `RuleBasedStateMachine`. You declare faults and rules — ordeal auto-injects a **nemesis** that toggles faults during exploration. The nemesis is just another Hypothesis rule, so the engine explores fault schedules like it explores any other state transition. Shrinking works automatically.

```python
from ordeal import ChaosTest, rule, invariant
from ordeal.faults import io, numerical, timing

class StorageChaos(ChaosTest):
    faults = [
        io.error_on_call("myapp.storage.save", IOError),
        timing.intermittent_crash("myapp.worker.process", every_n=3),
        numerical.nan_injection("myapp.scoring.predict"),
    ]
    swarm = True  # random fault subsets per run — better aggregate coverage

    @rule()
    def write_data(self):
        self.service.save({"key": "value"})

    @rule()
    def read_data(self):
        result = self.service.load("key")
        always(result is not None, "reads never return None after write")
```

Swarm mode: each run activates a random subset of faults. Over many runs, this covers more fault combinations than always-all-on. Hypothesis handles the subset selection, so shrinking isolates the exact fault combination that triggers a failure.

### Property assertions

Four property types, plus an optional declaration helper for deferred checks:

```python
from ordeal import always, declare, sometimes, reachable, unreachable

always(len(results) > 0, "never empty")          # must hold every time — fails immediately
sometimes(cache_hit, "cache is used")             # must hold at least once — checked at session end
declare("error-recovery-path", "reachable")       # declare deferred expectation up front
reachable("error-recovery-path")                  # code path must execute at least once
unreachable("silent-data-corruption")             # code path must never execute — fails immediately
```

`always` and `unreachable` fail instantly, triggering Hypothesis shrinking. `sometimes` and `reachable` accumulate evidence across the full session. Use `declare()` when you want those deferred properties to fail even if the marker was never observed.

### Inline fault injection (BUGGIFY)

Place `buggify()` gates in your production code. They return `False` normally. During chaos testing, they probabilistically return `True`:

```python
from ordeal.buggify import buggify, buggify_value

def process(data):
    if buggify():                                    # sometimes inject delay
        time.sleep(random.random() * 5)
    result = compute(data)
    return buggify_value(result, float('nan'))        # sometimes corrupt output
```

Seed-controlled. Thread-local. No-op when inactive. This is [FoundationDB's BUGGIFY](https://apple.github.io/foundationdb/testing.html) for Python — the code under test *is* the test harness.

### Coverage-guided exploration

The Explorer tracks which code paths each run discovers (AFL-style edge hashing). When a run finds new coverage, it saves a checkpoint. Future runs branch from high-value checkpoints, systematically exploring the state space:

```python
from ordeal.explore import Explorer

explorer = Explorer(
    MyServiceChaos,
    target_modules=["myapp"],
    checkpoint_strategy="energy",  # favor productive checkpoints
)
result = explorer.run(max_time=60)
print(result.summary())
# Exploration: 5000 runs, 52000 steps, 60.0s
# Coverage: 287 edges, 43 checkpoints
# Failures found: 2
#   Run 342, step 15: ValueError (3 steps after shrinking)
```

Failures are **shrunk** — delta debugging removes unnecessary steps, then fault simplification removes unnecessary faults. You get the minimal sequence that reproduces the bug.

Scale with `workers` — each process gets a unique seed for independent exploration, results are aggregated:

```python
explorer = Explorer(MyServiceChaos, target_modules=["myapp"], workers=8)
```

### Configuration

```toml
# ordeal.toml — one file, human and machine readable
[explorer]
target_modules = ["myapp"]
max_time = 60
seed = 42
checkpoint_strategy = "energy"

[[tests]]
class = "tests.test_chaos:MyServiceChaos"

[report]
format = "both"
traces = true
verbose = true
```

See [`ordeal.toml.example`](ordeal.toml.example) for the full schema with every option documented.

### QuickCheck with boundary bias

`@quickcheck` infers strategies from type hints. It biases toward boundary values — 0, -1, empty list, max length — where implementation bugs cluster:

```python
from ordeal.quickcheck import quickcheck

@quickcheck
def test_sort_idempotent(xs: list[int]):
    assert sorted(sorted(xs)) == sorted(xs)

@quickcheck
def test_score_bounded(x: float, y: float):
    result = score(x, y)
    assert 0 <= result <= 1
```

### Composable invariants

```python
from ordeal.invariants import no_nan, no_inf, bounded, finite

valid_score = finite & bounded(0, 1)
valid_score(model_output)  # raises AssertionError with clear message
```

Invariants compose with `&`. Works with scalars, sequences, and numpy arrays.

### Simulation primitives

Deterministic Clock and FileSystem — no mocks, no real I/O, instant:

```python
from ordeal.simulate import Clock, FileSystem

clock = Clock()
fs = FileSystem()

clock.advance(3600)                      # instant — no real waiting
fs.inject_fault("/data.json", "corrupt") # reads return random bytes
```

### Differential testing

Compare two implementations on the same random inputs — catches regressions and validates refactors:

New to this? Start with [Differential Testing, Without Jargon](https://docs.byordeal.com/concepts/differential-testing/),
then follow the copy-paste [Differential Quickstart](https://docs.byordeal.com/guides/differential-quickstart/).

```python
from ordeal.diff import diff

result = diff(score_v1, score_v2, rtol=1e-6)
assert result.status == "no_divergence_observed", result.summary()
# diff(score_v1, score_v2): 100 examples, NO DIVERGENCE OBSERVED
```

Each side receives independent inputs and receiver state. The comparison covers
returns, exceptions, mutated arguments, receiver state, and selected restorable
side effects. A divergence carries one minimized, replay-verified witness;
sampled agreement remains bounded evidence, not equivalence.

Every divergence also exposes a source-bound JSON record in `result.artifacts`.
Pass `artifact_dir=".ordeal/divergences"` to persist the same canonical record.
Use [State and Side Effects](https://docs.byordeal.com/guides/differential-state-and-effects/)
for mutations, receivers, logs, caches, or other external state, and
[Evidence and Statuses](https://docs.byordeal.com/guides/differential-evidence/)
when reviewing a witness or deciding what a result proves.
For a story-led explanation of the durable card itself, read
[Divergence Evidence](https://docs.byordeal.com/concepts/divergence-evidence/),
then use its [workflow](https://docs.byordeal.com/guides/divergence-evidence/),
[troubleshooting](https://docs.byordeal.com/guides/divergence-evidence-troubleshooting/),
or [exact schema](https://docs.byordeal.com/reference/divergence-evidence-schema/).

For stateful systems, pass two zero-argument factories and one operation/fault
sequence. The same `diff()` call compares interface, outcomes, state, selected
side effects, recovery parity, and an optional separate performance budget:

```python
from ordeal.diff import FaultEvent, Operation, diff

result = diff(
    OldStore,
    NewStore,
    sequence=[FaultEvent("timeout"), Operation("read")],
    apply_fault=apply_fault,
)
```

If “operation-and-fault story” is new to you, start with the
[plain-language mental model](https://docs.byordeal.com/concepts/system-differential/),
then run the self-contained [first system comparison](https://docs.byordeal.com/guides/system-differential/).
The [recipes](https://docs.byordeal.com/guides/system-differential-recipes/)
cover APIs, state, effects, and budgets; the
[troubleshooting guide](https://docs.byordeal.com/guides/system-differential-troubleshooting/)
explains every common surprising result.

For a committed refactor, run both versions in isolated worktrees and
subprocesses while replaying the same inputs:

```bash
ordeal diff myapp.scoring --base-ref origin/main --candidate-ref HEAD --save-artifacts
```

Start with [Compare Two Git Revisions](https://docs.byordeal.com/guides/revision-diff/)
for the sealed-worktree mental model and first result. Use
[Revision Diff Troubleshooting](https://docs.byordeal.com/guides/revision-diff-troubleshooting/)
for refs, imports, fixtures, and inconclusive replay; the
[Revision Diff Schema](https://docs.byordeal.com/reference/revision-diff-schema/)
is the machine contract for saved JSON.

Replacing a whole module? Matching the old version is not enough: a perfect
copy can preserve an old bug. The migration workflow also checks explicit
business rules, test strength, and the replacement on its own:

```bash
ordeal migrate oldpkg.scoring newpkg.scoring -c ordeal.toml
```

Start with the layman-friendly [Safe Module Migrations](https://docs.byordeal.com/concepts/safe-migrations/),
then follow the [migration workflow](https://docs.byordeal.com/guides/migration-workflow/).

### Mutation testing

Validates that your chaos tests actually catch bugs. If you flip `+` to `-` in the code and your tests still pass, your tests have a blind spot:

```python
from ordeal.mutations import mutate_function_and_test

result = mutate_function_and_test("myapp.scoring.compute", my_tests)
print(result.summary())
print(result.test_protection_view())
# Mutation score: 15/18 (83%)
#   SURVIVED  L42:8 + -> -
#   SURVIVED  L67:4 negate if-condition
```

`test_protection_view()` combines the score, survivors, kill attribution, and
property strength into a scoped `weak`, `inconclusive`, or
`protective_within_measured_scope` verdict. It never treats line coverage alone
as proof that assertions are meaningful.

### Fault library

```python
from ordeal.faults import io, numerical, timing, network, concurrency

# I/O faults
io.error_on_call("mod.func")            # raise IOError
io.disk_full()                           # writes fail with ENOSPC
io.corrupt_output("mod.func")           # return random bytes
io.truncate_output("mod.func", 0.5)     # truncate to half

# Numerical faults
numerical.nan_injection("mod.func")      # output becomes NaN
numerical.inf_injection("mod.func")      # output becomes Inf
numerical.wrong_shape("mod.func", (1,512), (1,256))

# Timing faults
timing.timeout("mod.func")              # raise TimeoutError
timing.slow("mod.func", delay=2.0)      # add delay
timing.intermittent_crash("mod.func", every_n=3)
timing.jitter("mod.func", magnitude=0.01)

# Network faults
network.http_error("mod.client.post", status_code=503)
network.connection_reset("mod.client.post")
network.rate_limited("mod.client.get", retry_after=60)
network.dns_failure("mod.client.resolve")

# Concurrency faults
concurrency.contended_call("mod.pool.acquire", contention=0.1)
concurrency.thread_boundary("mod.cache.get")
concurrency.stale_state(service, "config", old_config)
```

### Integrations

```python
# Atheris — coverage-guided fuzzing steers buggify() decisions
from ordeal.integrations.atheris_engine import fuzz
fuzz(my_function, max_time=60)

# API chaos testing (built-in, no extra install)
from ordeal.integrations.openapi import chaos_api_test
chaos_api_test("http://localhost:8080/openapi.json", faults=[...])
chaos_api_test("http://localhost:8080/openapi.json", faults=[...], stateful=True)
```

### Audit — justify adoption with data

```bash
ordeal audit myapp.scoring --test-dir tests/
ordeal audit myapp.scoring --validation-mode deep
```

```
myapp.scoring
  current suite:         33 tests | 343 lines | 98% coverage [verified]
  generated incremental: 12 tests | 130 lines | 100% coverage [verified]
  mined:    compute: output in [0, 1] (500/500, >=99% CI)
  mutation: 14/18 (78%)
  protection: WEAK: 100% line coverage but 4/18 mutation(s) survived
  suggest:
    - L42 in compute(): test when x < 0
    - L67 in normalize(): test that ValueError is raised
```

Every number is either `[verified]` (measured and cross-checked) or `FAILED: reason` — the audit never silently returns 0%. The protection line is mutation-first: a surviving mutant keeps the verdict weak even at 100% line coverage. The verdict applies to the generated/migrated checks; use `ordeal mutate` to measure the selected existing pytest tests directly.

Read [Are your tests meaningful?](https://docs.byordeal.com/concepts/test-meaningfulness/)
for the plain-language model, then use the
[Test Protection Guide](https://docs.byordeal.com/guides/test-protection/) or
[CI Guide](https://docs.byordeal.com/guides/test-protection-ci/) for the full workflow.

## CLI

```bash
ordeal audit myapp.scoring              # compare existing tests vs ordeal
ordeal migrate oldpkg newpkg -c ordeal.toml  # ordered replacement validation
ordeal explore                          # run from ordeal.toml
ordeal explore -w 8                     # parallel with 8 workers
ordeal explore -c ci.toml -v            # custom config, verbose
ordeal explore --max-time 300 --seed 99 # override settings
ordeal explore --runner compose        # persistent services + real worker faults
ordeal replay trace.json                # reproduce a failure
ordeal replay --shrink trace.json       # minimize a failure trace
ordeal explore --generate-tests tests/test_gen.py  # turn traces into pytest tests
```

## Find what you need

Every goal maps to a starting point — a command to run, a module to import, and a page to read. Nothing is hidden.

| I want to... | Start here | In the codebase | Docs |
|---|---|---|---|
| Find bugs without writing tests | `ordeal scan mymodule` | `ordeal/auto.py` | [Scan Quickstart](https://docs.byordeal.com/guides/scan-quickstart/) |
| Turn a failure into a CI-guarded regression | `ordeal scan mymodule --save-artifacts` | `ordeal/finding_evidence.py` | [Durable Regression Workflow](https://docs.byordeal.com/guides/durable-regressions/) |
| Prove whether my tests protect behavior | `ordeal audit mymodule` | `ordeal/audit.py` | [Test Protection](https://docs.byordeal.com/guides/test-protection/) |
| Write a chaos test | `from ordeal import ChaosTest` | `ordeal/chaos.py` | [Getting Started](https://docs.byordeal.com/getting-started) |
| Inject specific failures (timeout, NaN, ...) | `from ordeal.faults import timing` | `ordeal/faults/` directory | [Fault Injection](https://docs.byordeal.com/concepts/fault-injection) |
| Explore all failure combinations | `ordeal explore` | `ordeal/explore.py` | [Explorer](https://docs.byordeal.com/guides/explorer) |
| Explore long-lived services | `ordeal explore --runner compose` | `ordeal/compose.py` | [Compose Services](https://docs.byordeal.com/guides/compose-runner) |
| Reproduce and shrink a failure | `ordeal replay trace.json` | `ordeal/trace.py` | [Shrinking](https://docs.byordeal.com/concepts/shrinking) |
| Add fail-safe gates to production code | `from ordeal.buggify import buggify` | `ordeal/buggify.py` | [Fault Injection](https://docs.byordeal.com/concepts/fault-injection) |
| Make assertions across all runs | `from ordeal import always, sometimes` | `ordeal/assertions.py` | [Assertions](https://docs.byordeal.com/concepts/property-assertions) |
| Control time / filesystem in tests | `from ordeal.simulate import Clock` | `ordeal/simulate.py` | [Simulation](https://docs.byordeal.com/guides/simulate) |
| Compare two function implementations | `diff(old, new)` | `ordeal/diff.py` | [Differential Quickstart](https://docs.byordeal.com/guides/differential-quickstart) |
| Validate a refactor across revisions | `ordeal diff mypkg.scoring` | `ordeal/_revision_diff.py` | [Divergence Evidence](https://docs.byordeal.com/concepts/divergence-evidence/) |
| Compare stateful system refactors | `diff(Old, New, sequence=[...])` | `ordeal/system_diff.py` | [System Learning Path](https://docs.byordeal.com/concepts/system-differential) |
| Replace a module without copying old bugs | `migrate("old.mod", "new.mod", ...)` | `ordeal/migration.py` | [Safe Module Migrations](https://docs.byordeal.com/concepts/safe-migrations/) |
| Discover round trips between functions | `ordeal mine-pair mod.fn1 mod.fn2` | `ordeal/mine.py` | [Auto Testing](https://docs.byordeal.com/guides/auto) |
| Compose validation rules | `from ordeal.invariants import no_nan` | `ordeal/invariants.py` | [API Reference](https://docs.byordeal.com/reference/api) |
| Test API endpoints for faults | `from ordeal.integrations.openapi import chaos_api_test` | `ordeal/integrations/openapi.py` | [Integrations](https://docs.byordeal.com/guides/integrations) |
| Extend ordeal with a new fault | Follow the pattern in `faults/*.py` | `ordeal/faults/` | [Fault Injection](https://docs.byordeal.com/concepts/fault-injection) |
| Configure reproducible runs | Create `ordeal.toml` | `ordeal/config.py` | [Configuration](https://docs.byordeal.com/guides/configuration) |
| Discover all faults, assertions, strategies | `from ordeal import catalog; catalog()` | `ordeal/__init__.py` | [API Reference](https://docs.byordeal.com/reference/api) |

> **New to ordeal?** Start with `ordeal mine ordeal.demo` to see it in action, then read [Getting Started](https://docs.byordeal.com/getting-started).
> **Have existing tests?** Run `ordeal audit mymodule --test-dir tests/` to see how they compare.
> **Want the full picture?** Browse the [full documentation](https://docs.byordeal.com/).**

## Architecture — code map

Every module does one thing. When you want to understand, use, or extend ordeal, this tells you where to look.

```
ordeal/
├── chaos.py           Your tests extend this — ChaosTest base class, nemesis, swarm mode
├── explore.py         The exploration engine — coverage tracking, checkpoints, energy scheduling
├── assertions.py      always / sometimes / reachable / unreachable — the assertion model
├── buggify.py         Inline fault gates — thread-local, seed-controlled, no-op when inactive
├── quickcheck.py      @quickcheck — type-driven strategies with boundary bias
├── simulate.py        Deterministic Clock and FileSystem — no mocks, no real I/O
├── invariants.py      Composable checks — no_nan & bounded(0, 1), works with numpy
├── mutations.py       AST mutation testing — 14 operators, count-and-apply pattern
├── auto.py            Auto-testing — scan_module, fuzz, mine, diff, chaos_for
├── finding_evidence.py Bounded claims, witness hashes, replay and fix controls
├── regression_evidence.py AST bindings for generated witness regressions
├── trace.py           Trace recording, JSON serialization, replay, delta-debugging shrink
├── compose.py         Long-lived Compose services, faults, state, exact trace and replay counts
├── system_diff.py     Stateful refactor parity, fault/recovery replay, performance budgets
├── migration.py       Ordered audit/mine/diff/regression/mutation/scan migration gate
├── config.py          ordeal.toml loader — strict validation
├── cli.py             CLI entry point — explore, replay, mine, audit
├── plugin.py          Pytest plugin — --chaos, --chaos-seed, --buggify-prob
├── strategies.py      Adversarial Hypothesis strategies for fuzzing
├── faults/            All fault types, organized by what can go wrong:
│   ├── io.py              disk_full, corrupt_output, permission_denied
│   ├── numerical.py       nan_injection, inf_injection, wrong_shape
│   ├── timing.py          timeout, slow, intermittent_crash, jitter
│   ├── network.py         http_error, connection_reset, dns_failure
│   └── concurrency.py     contended_call, thread_boundary, stale_state
└── integrations/      Optional bridges to specialized tools:
    ├── openapi.py         Built-in API chaos testing (no extra deps)
    └── atheris_engine.py  Coverage-guided fuzzing (pip install ordeal[atheris])
```

> **Want to add a new fault?** Look at any function in `ordeal/faults/` — they all follow the same pattern: take a dotted target path, return a `PatchFault` or `LambdaFault`. Adding a new fault type means adding a new function that follows this pattern.
>
> **Want to understand how something works?** Every module is self-contained. Read the module you're interested in — the code matches the documentation.

## License

Apache 2.0
