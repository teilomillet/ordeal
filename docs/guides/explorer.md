---
description: >-
  Run ordeal's coverage-guided Explorer to find deep bugs. Configure
  ordeal.toml, launch parallel workers, checkpoint interesting states,
  generate regression tests.
---

# Explorer

!!! quote "The big picture"
    The Explorer is your automated bug hunter. You point it at your code, it systematically searches for failures by running hundreds of thousands of action sequences, watching which code paths get executed, and steering toward the ones it hasn't seen before. Think of it as a tireless QA engineer who never gets bored, never forgets what it already tried, and gets smarter the longer it runs.

    The engine lives in `ordeal/explore.py` and is configured via `ordeal.toml`. The workflow is simple: **configure, run, read results, fix, repeat.**

The Explorer is ordeal's coverage-guided exploration engine. It runs your ChaosTest classes with code coverage feedback, checkpointing interesting states and branching from them to reach deep, rare bugs that random testing misses.

This page covers practical usage. For the theory behind edge coverage, energy scheduling, and why coverage guidance finds bugs that random testing cannot, see [Coverage Guidance](../concepts/coverage-guidance.md).

---

## When to use the Explorer

Hypothesis is ordeal's default execution engine. When you run `pytest --chaos`, Hypothesis explores rule interleavings and fault schedules randomly, with algebraic shrinking when it finds a failure. For most day-to-day testing, that is enough.

The Explorer adds a layer on top: **coverage feedback**. It watches which code paths your system actually executes, saves states that reach new paths, and biases future exploration toward those states. This matters when:

- **Hypothesis plateaus.** You have been running ChaosTest for a while and it stopped finding new bugs. The random exploration has covered the easy paths and cannot reach the deep ones.
- **Bugs live at feature intersections.** The failure requires a specific sequence of 5+ actions to trigger -- a timeout during a retry, during a batch write, while the cache is cold. Random selection needs millions of runs to stumble into this. The Explorer checkpoints at step 3 and only needs to find the remaining steps from there.
- **Pre-release validation.** You want to go deeper than CI allows. Give the Explorer 30 minutes before a release and let it tunnel into corners of your state space that fast tests never reach.
- **You suspect there are bugs but cannot write a targeted test.** Coverage guidance does not need you to know where the bug is. It finds new code paths automatically and explores variations of the sequences that produce them.

If your ChaosTest already fails reliably under `pytest --chaos`, you do not need the Explorer. Fix the bugs Hypothesis finds first. Run the Explorer when Hypothesis stops finding things.


## Quick start

!!! quote "In plain English"
    You can start the Explorer two ways: write a few lines of Python, or create a small config file and run a CLI command. Either way, you get back a report telling you how many code paths were found, how many bugs surfaced, and exactly how to reproduce each one.

### Python API

```python
from ordeal.explore import Explorer

explorer = Explorer(
    MyServiceChaos,
    target_modules=["myapp"],
)
result = explorer.run(max_time=60)
print(result.summary())
```

`result` is an `ExplorationResult` with fields: `total_runs`, `total_steps`, `unique_edges`, `checkpoints_saved`, `failures`, `duration_seconds`, `traces`.

### CLI

Create an `ordeal.toml` in your project root:

```toml
[explorer]
target_modules = ["myapp"]
max_time = 60

[[tests]]
class = "tests.test_chaos:MyServiceChaos"

[report]
verbose = true
```

Then run:

```bash
ordeal explore -v
```

The `-v` flag enables live progress output. You will see a line like this, updated every two seconds:

```
  [12s] runs=5832 steps=148201 edges=47 cps=31 fails=0 (486 runs/s)
```

Override settings from the command line without editing the TOML:

```bash
ordeal explore --max-time 300 --seed 99 -v
```

Save exploration state for later resumption:

```bash
ordeal explore -v --save-state .ordeal/state.pkl
# ... interrupt or let it finish, then resume:
ordeal explore -v --resume .ordeal/state.pkl --save-state .ordeal/state.pkl
```

Resuming restores the checkpoint corpus, discovered edges, and RNG state so exploration continues where it left off instead of starting from scratch.


## Configuration reference

!!! quote "Why this matters"
    Every setting below is a knob that controls how the Explorer searches. You do not need to touch most of them — the defaults are tuned for common use cases. But when you want to go deeper, run faster, or focus on a specific part of your code, these knobs let you shape the search. Think of it like adjusting a microscope: the defaults give you a good view, and the settings let you zoom in or out.

All settings live in `ordeal.toml` under the `[explorer]` section. Every parameter has a default, so you only need to set the ones you want to change. For the full configuration schema including `[[tests]]` and `[report]` sections, see [Configuration](configuration.md).

### `target_modules`

**Type:** `list[str]` -- **Default:** `[]`

Which Python modules to instrument for edge coverage. The Explorer uses `sys.settrace` to track control-flow transitions inside these modules. This is how it knows whether a run discovered new code paths.

```toml
target_modules = ["myapp"]              # track everything in myapp/
target_modules = ["myapp.core"]         # track only the core subpackage
target_modules = ["myapp", "mylib"]     # track multiple packages
```

**Guidance:**

- Set this to your **application code**, not your test code. Tracking test code adds noise without useful signal -- the Explorer would checkpoint based on which test branches ran, not which application paths were exercised.
- Start narrow. If your application is `myapp` with subpackages `myapp.core`, `myapp.api`, `myapp.cache`, start with `target_modules = ["myapp.core"]`. If the edge count plateaus too quickly, widen to `["myapp"]`.
- More modules means more tracing overhead. Every line event in a tracked module goes through a Python callback. For small modules (a few hundred lines), this is negligible. For large codebases (tens of thousands of lines), it can slow exploration by 2-5x. The tradeoff is: wider tracking finds more bugs but runs fewer iterations per second.
- If `target_modules` is empty, the Explorer still runs but has no coverage feedback. It becomes a random explorer with checkpointing -- better than nothing, but you lose the main benefit.

### `max_time`

**Type:** `float` -- **Default:** `60`

Wall-clock time limit in seconds. The Explorer stops starting new runs after this duration. Any run in progress when the time expires will complete, and shrinking happens afterward.

```toml
max_time = 60       # local development: quick feedback
max_time = 300      # CI: thorough but bounded
max_time = 3600     # pre-release: go deep
```

**Guidance:**

- For local development, 30-60 seconds gives fast feedback. You will see whether edge discovery is progressing and whether any failures surface.
- For CI, 300 seconds (5 minutes) is a reasonable balance. Long enough to find deep bugs, short enough to not block your pipeline.
- For pre-release or periodic deep runs, 1800-3600 seconds (30-60 minutes) lets the Explorer really tunnel into rare states. Run this on a schedule (nightly, weekly) rather than on every push.
- The `max_runs` parameter (default: `null`) provides a run-count limit as an alternative to time. When both are set, whichever is reached first stops exploration.

### `checkpoint_strategy`

!!! quote "The key insight"
    The Explorer saves snapshots of interesting states it discovers — these are checkpoints. When it starts a new run, it can pick up from one of those saved states instead of starting from scratch. The checkpoint strategy controls **which** saved state it picks. The default, `"energy"`, means the Explorer learns from experience: states that led to new discoveries in the past get picked more often. It is how the Explorer gets smarter over time.

**Type:** `str` -- **Default:** `"energy"`

How the Explorer picks which checkpoint to branch from.

| Strategy | Selection method | Best for |
|---|---|---|
| `"energy"` | Weighted by energy score. Checkpoints that led to new edges have higher energy. | General use. This is the default and works well for most codebases. |
| `"uniform"` | All checkpoints are equally likely. | When you suspect energy scheduling is getting stuck -- repeatedly selecting the same checkpoint without progress. Uniform gives every checkpoint a fair chance. |
| `"recent"` | Newer checkpoints are preferred, weighted linearly by creation order. | Systems with deep sequential state where the most recent discoveries are the most promising starting points. |

**Guidance:**

- Start with `"energy"`. It is the default for a reason -- the energy system naturally balances exploitation (branching from productive checkpoints) with exploration (energy decay ensures old checkpoints eventually lose priority).
- Switch to `"uniform"` if you notice that edges plateau early and the Explorer seems to be repeatedly selecting the same few checkpoints. You can tell by watching the `cps` count -- if it stops growing while `runs` keeps climbing, the Explorer may be stuck.
- Use `"recent"` for systems where state builds up over time and the deepest state is the most interesting. Think of a database that accumulates records, or a cache that fills up. The newest checkpoints represent the deepest states.

### `checkpoint_prob`

**Type:** `float` -- **Default:** `0.4`

Probability that each run starts from a saved checkpoint rather than from a fresh ChaosTest instance.

At 0.4 (the default), 40% of runs branch from a checkpoint and 60% start fresh.

**Guidance:**

- Higher values (0.6-0.8) push the Explorer deeper. Most runs continue from known interesting states, exploring variations and extensions. Use this when you have already found interesting checkpoints and want to go deeper from them.
- Lower values (0.1-0.3) give more fresh starts. The Explorer spends more time exploring from scratch, which can find bugs that only manifest in clean initial states. Use this if you suspect shallow bugs or if your ChaosTest has many rules that interact differently from a fresh state.
- At 0.0, the Explorer never uses checkpoints. It becomes a coverage-guided random tester -- still tracks edges, but never branches from saved states. This is rarely what you want.
- At 1.0, the Explorer never starts fresh (after the first run). It always branches from checkpoints. This can lead to deep exploration but may miss bugs that require a fresh state.

### `steps_per_run`

**Type:** `int` -- **Default:** `50`

Maximum number of rule steps per exploration run. Each run executes a random number of steps between 1 and this value.

**Guidance:**

- 50 is a good default for most systems. A typical ChaosTest has 3-10 rules, so 50 steps means roughly 5-15 executions of each rule per run.
- Increase to 100-200 for systems with deep state that takes many steps to build up. If your system needs 30 operations to reach an interesting state, 50 steps only leaves 20 for exploring from there.
- Decrease to 10-25 for fast iteration during development. Shorter runs mean more runs per second, which means faster feedback on whether your ChaosTest is set up correctly.
- This can be overridden per test class in the `[[tests]]` section of `ordeal.toml`.

### `fault_toggle_prob`

**Type:** `float` -- **Default:** `0.3`

Probability of toggling a fault (nemesis action) at each step, instead of executing a rule.

At 0.3, roughly 30% of steps are fault toggles and 70% are rule executions.

**Guidance:**

- Higher values (0.4-0.6) mean faults are more active. Rules and faults interleave more frequently, which is good for finding bugs caused by faults firing at specific moments (mid-operation failures, concurrent fault scenarios).
- Lower values (0.1-0.2) mean longer fault-free windows between toggles. The system has more time to build up state between fault events. Good for finding bugs that require complex state to manifest, where too many faults just reset or break the state before it gets interesting.
- At 0.0, no faults are ever toggled. The Explorer becomes a pure rule explorer. Useful for debugging rule interactions without fault noise.

### `max_checkpoints`

**Type:** `int` -- **Default:** `256`

Maximum number of checkpoints in the corpus. When the corpus is full, the lowest-energy checkpoint is evicted to make room (when using the `"energy"` strategy; random eviction for other strategies).

**Guidance:**

- 256 is a reasonable default. It is large enough to maintain diversity but small enough to keep memory usage bounded.
- Each checkpoint stores a snapshot of your ChaosTest instance's state dict (skipping heavy Fault objects). If your test class holds large user state (big data structures, many objects), each checkpoint consumes that much memory. Monitor memory usage if you increase this significantly.
- Increase to 512-1024 for systems with many distinct interesting states. If you see the edge count still climbing when the checkpoint corpus is already full, you may benefit from keeping more checkpoints alive.
- Decrease to 64-128 for memory-constrained environments or when each checkpoint is large.

### `seed`

**Type:** `int` -- **Default:** `42`

RNG seed for the Explorer. Controls all random decisions: checkpoint selection, rule selection, fault toggles, parameter draws, and the number of steps per run.

**Guidance:**

- Same seed + same code = same exploration sequence. Use this for reproducibility.
- In CI, set `seed = 0` or another fixed value so runs are deterministic. If a CI run finds a failure, you can reproduce it locally with the same seed.
- For local development, you can vary the seed across runs to get different exploration paths: `ordeal explore --seed $RANDOM`.
- The seed is recorded in every trace file, so even if you forget what seed you used, you can recover it from the trace.

### `workers`

!!! quote "What you can do with this"
    Instead of one Explorer searching alone, you can have many running at the same time across your CPU cores. They share what they find so nobody wastes time re-discovering the same code paths. More workers means more ground covered in less time. On a typical 8-core machine, you get roughly 5-7x the throughput of a single worker.

**Type:** `int` -- **Default:** `0` (auto: uses all CPU cores)

Number of parallel worker processes. Each worker explores independently with a unique seed. Set to `1` for sequential exploration, or `0` (the default) to automatically use all available CPU cores.

```toml
workers = 0       # auto: os.cpu_count()
workers = 1       # sequential (no multiprocessing)
workers = 8       # explicit 8 workers
```

Override from the CLI: `ordeal explore -w 4`.

Workers share discoveries through two mechanisms enabled by default:

- **Shared edge bitmap** (`share_edges`): a 64KB shared-memory buffer where each byte marks a discovered edge. Workers skip edges already found by others. Zero locks -- single-byte writes are atomic. This causes workers to naturally diverge into different regions of the state space.
- **Shared checkpoint pool** (`share_checkpoints`): workers publish significant discoveries as pickle files to a shared temp directory. Other workers load these checkpoints and branch from states they would never have reached independently. Disable with `share_checkpoints = false` if the pickle overhead is too high for your state.


## Reading the output

!!! quote "Think of it this way"
    The Explorer talks to you in two ways. While it is running, it shows a live ticker with key numbers — how many runs it has done, how many new code paths it found, and whether it hit any bugs. When it finishes, it gives you a summary with every failure it found and how many steps it takes to reproduce each one. Learning to read these numbers is the fastest way to know whether the Explorer is making progress or stuck.

### Live progress line

When running with `-v` or `verbose = true`:

```
  [12s] runs=5832 steps=148201 edges=47 cps=31 fails=0 (486 runs/s)
```

| Field | Meaning |
|---|---|
| `[12s]` | Wall-clock time elapsed since exploration started. |
| `runs=5832` | Total exploration runs completed. Each run is one sequence of rule steps, starting either fresh or from a checkpoint. |
| `steps=148201` | Total rule steps executed across all runs. This is the sum of individual step counts. |
| `edges=47` | Unique control-flow edges discovered so far. This is the cumulative count. New edges trigger checkpoints. |
| `cps=31` | Checkpoints currently saved. This grows as new edges are found and caps at `max_checkpoints`. |
| `fails=0` | Failures (unhandled exceptions) found so far. |
| `486 runs/s` | Throughput. Runs per second. Depends on your system's complexity, the number of target modules, and step count per run. |

**What to watch for:**

- **Edges climbing steadily:** Good. The Explorer is finding new code paths. Let it run.
- **Edges flat for a long time:** The Explorer may have exhausted reachable paths with the current configuration. Try adding more `target_modules`, increasing `steps_per_run`, or increasing `max_time`.
- **Checkpoints growing:** Good. The Explorer is finding diverse interesting states.
- **Failures appearing:** The Explorer found a bug. It will continue exploring (it does not stop on the first failure) and shrink all failures at the end.

### Final summary

After exploration completes, the Explorer prints a summary:

```
--- Ordeal Exploration Report ---

  tests.test_chaos:MyServiceChaos
    58320 runs, 1482010 steps, 300.0s
    47 edges, 31 checkpoints
    2 FAILURES:
      ValueError: balance cannot be negative (3 steps)
      TimeoutError: connection timed out during write (7 steps)
```

The step counts in parentheses after each failure are the **shrunk** trace lengths -- the minimal number of steps needed to reproduce the failure.


## Shrinking

!!! quote "Why this matters"
    When the Explorer finds a bug, the sequence that triggered it is usually long and full of irrelevant steps. Shrinking strips away everything that does not matter, leaving you with the **shortest possible recipe** to reproduce the failure. A 50-step mess becomes a clean 3-step story: "do this, then this, then it breaks." That is the difference between a confusing stack trace and an obvious bug report.

When the Explorer finds failures, it shrinks each one after exploration completes. Shrinking reduces a failing trace to the minimal sequence of steps that still reproduces the failure.

The shrinking process has three phases, applied iteratively until no more steps can be removed:

1. **Delta debugging.** Remove large chunks (halves, quarters, eighths) and check if the failure still reproduces. This quickly eliminates large irrelevant prefixes and suffixes.
2. **Single-step elimination.** Try removing each remaining step individually. This catches steps that delta debugging missed because they were in a chunk with essential steps.
3. **Fault simplification.** For each fault that was toggled during the trace, remove all its toggle events and check if the failure still reproduces. This strips out faults that were not involved in causing the failure.

A 50-step trace might shrink to 3 steps. The shrunk trace tells you exactly what sequence of actions triggers the bug, with no noise.

### Controlling shrinking

By default, shrinking runs with a 30-second time limit per failure. You can skip it entirely for faster exploration:

```bash
ordeal explore -v --no-shrink
```

If you skip shrinking during exploration, you can shrink traces later:

```bash
ordeal replay --shrink .ordeal/traces/fail-run-42.json -o minimal.json
```

Or from Python:

```python
from ordeal.trace import Trace, shrink

trace = Trace.load(".ordeal/traces/fail-run-42.json")
minimal = shrink(trace, MyServiceChaos, max_time=60.0)
minimal.save("minimal.json")
```


## Traces

!!! quote "In plain English"
    A trace is a complete recording of everything the Explorer did during a run — every action, every parameter, every fault toggle. It is saved as a simple JSON file. You can replay it to reproduce a failure on any machine, share it with a teammate, or shrink it down to the minimum steps. Think of traces as bug receipts: they prove exactly how a failure happened and let anyone reproduce it. The trace system lives in `ordeal/trace.py`.

Every failure found by the Explorer is recorded as a JSON trace file. A trace captures every decision made during the run: which rules fired, what parameters were drawn, which faults were toggled, and at which step the failure occurred.

Traces are saved when `traces = true` in the `[report]` section:

```toml
[report]
traces = true
traces_dir = ".ordeal/traces"
```

Failure traces are saved as `fail-run-{run_id}.json` in the traces directory. For compact storage, use a `.json.gz` suffix — traces are gzip-compressed automatically (typically 10-40x smaller).

### Replaying a trace

From the CLI:

```bash
ordeal replay .ordeal/traces/fail-run-42.json
```

If the failure reproduces, the output shows the error. If it does not (e.g., due to code changes or nondeterminism), the output says so.

From Python:

```python
from ordeal.trace import Trace, replay

trace = Trace.load(".ordeal/traces/fail-run-42.json")
error = replay(trace)
if error is not None:
    print(f"Reproduced: {error}")
else:
    print("Did not reproduce")
```

### Shrinking a trace

```bash
ordeal replay --shrink .ordeal/traces/fail-run-42.json -o minimal.json
```

From Python:

```python
from ordeal.trace import Trace, shrink

trace = Trace.load("fail-run-42.json")
minimal = shrink(trace, MyServiceChaos)
minimal.save("minimal.json")
print(f"Shrunk from {len(trace.steps)} to {len(minimal.steps)} steps")
```

### Sharing traces

Trace files are self-contained JSON. They record the test class path, seed, all steps with parameters, and the failure info. You can commit them to your repository, attach them to issues, or pass them to a colleague. Anyone with the same codebase can replay them:

```bash
# On another machine with the same code:
ordeal replay fail-run-42.json
```

The trace file includes the `test_class` field (e.g., `"tests.test_chaos:MyServiceChaos"`), so the replay command knows which class to import and instantiate. No additional configuration is needed.


## Unified swarm

Swarm testing (Groce et al., ISSTA 2012) improves fault detection by deliberately omitting features from each test run. The insight is that features can **actively suppress** bugs — deletes prevent a cache from growing large enough to trigger GC, close calls mask file descriptor exhaustion, pop operations keep a stack from overflowing. By randomly disabling some features per run, the remaining ones dominate and push the system into states that all-features-on testing rarely reaches.

The paper validated this on YAFFS2, five production C compilers, and Sglib — 42% more distinct crash signatures than the default all-features-on configuration over one week of testing.

ordeal extends the paper's algorithm with two additional layers:

### Architecture

```
Layer 3: Coverage-directed (steer)
    Rules that exercise files with uncovered branches → boosted inclusion
    ↓ biases configuration probabilities
Layer 2: Adaptive energy (exploit)
    Configs that found new edges → energy boost, slower decay
    ↓ weights configuration selection
Layer 1: Unified coin-flip (explore)
    Independent Bernoulli(0.5) per feature → joint rule+fault config
    ↓ per-run configuration
Explorer main loop
```

**Layer 1 — Coin-flip.** Each rule and each fault is included with independent probability 0.5. One bitmask covers both dimensions — a "configuration" in the paper's sense. At least one rule is always retained.

**Layer 2 — Adaptive energy.** After a 20-run warmup, the Explorer tracks which configurations led to new edge coverage. Productive configs get a 2× energy boost; never-productive ones decay at 0.9/run to a floor of 0.1. Previously productive configs decay slowly (0.98/run) so the Explorer doesn't abandon configurations that found edges early but haven't found new ones recently.

**Layer 3 — Coverage-directed.** When coverage gap data is available, the Explorer identifies which rules exercise files containing unreached branches (via the `_rule_file_coverage` map built during exploration). Those rules get 80% inclusion probability vs 30% for others. This closes the feedback loop: gaps → steer → reach gap → new edges.

### Post-warmup allocation

| Share | Strategy | Purpose |
|---|---|---|
| 10% | C_D (full config) | Paper §2.2 mitigation — all rules + all faults. Guarantees sequence bugs remain findable. |
| 15% | Coin-flip | Fresh diverse configurations. Accumulation/suppression bugs. |
| 10% | Coverage-directed | Bias toward rules that exercise uncovered files. |
| 65% | Energy-weighted | Exploit productive configurations from history. |

### Tradeoffs

Swarm is strictly better for **suppression bugs** (one feature preventing another from reaching its failure mode). It is worse for **sequence bugs** that require all features in a specific order — the C_D reservation mitigates this but does not eliminate it. Empirically on ordeal's own test classes: swarm finds 77% more accumulation bugs (`_CacheWithGC`: 367 vs 207) while finding 76% fewer sequence bugs (`_ComplexBomb`: 91 vs 385). The aggregate across diverse bug types favors swarm.

### Configuration

```toml
[explorer]
rule_swarm = true
```

```python
Explorer(MyServiceChaos, target_modules=["myapp"], rule_swarm=True)
```

Traces record the active configuration per run:

```
[swarm rules=2 faults=1]
rule: insert
rule: insert
...
```


## Seed corpus

The seed corpus is a persistent regression cache modeled after Go's `testdata/fuzz/` directory and Hypothesis's `.hypothesis/` database. Failing traces are content-addressed and saved to disk; subsequent test runs replay them before doing anything else.

### Mechanism

1. **Capture.** After shrinking, each failing trace is saved to `.ordeal/seeds/<TestClass>/seed-<sha256[:12]>.json`. Content addressing prevents duplicates.

2. **Replay.** The pytest plugin replays all saved seeds on *every* `pytest` invocation — not just `--chaos` runs. This is the Go fuzzing model: crashers become permanent regression tests with zero user action.

3. **Lifecycle.** Seeds that no longer reproduce are reported as `fixed` but kept on disk (the code change may be reverted). `ordeal seeds --prune-fixed` cleans them up.

### CLI

```bash
ordeal seeds                 # list all seeds, replay each, show status
ordeal seeds --prune-fixed   # remove seeds that no longer reproduce
ordeal explore --no-seeds    # skip seed replay during exploration
```

### pytest output

```
===== Ordeal Seed Corpus =====
  REGRESSION  seed-a3f2c1: tests.chaos:MyService — ValueError: balance negative
  fixed       seed-b4e3d2: tests.chaos:MyService — no longer reproduces

  1 regression(s) still reproduce
```

### Configuration

```toml
[report]
corpus_dir = ".ordeal/seeds"   # default
```


## Fault ablation

Shrinking minimizes the *step sequence*. Fault ablation minimizes the *fault set*. After shrinking, the Explorer determines which faults are necessary for each failure to reproduce — the root-cause question from Antithesis: *"Is it possible to find the bug without fiddling with the clock?"*

### Algorithm

**Phase 1 — Individual ablation.** For each fault F in the trace, replay without F's toggles (all other faults remain). If the failure disappears, F is individually necessary.

**Phase 2 — Pairwise ablation.** If faults A and B both appear individually unnecessary (removing either alone still fails, because the other compensates), replay without both A and B. If the failure disappears, A and B are *jointly necessary*. This catches the pattern where either fault alone is sufficient but the bug requires at least one.

**Epistemic scope.** Ablation identifies faults necessary for *this specific trace*. A different execution path might reproduce the same bug with a different fault set.

### Output

```
Run 3, step 5: ValueError: balance negative
  Active faults: timeout, disk_full, rate_limit
  Necessary faults: timeout, disk_full
  Sequence: call_api -> write_data -> call_api
```

### Usage

```bash
ordeal replay --ablate trace.json
ordeal replay --shrink --ablate trace.json
```

```python
from ordeal import ablate_faults
faults = ablate_faults(trace)
# {"timeout": True, "disk_full": True, "rate_limit": False}
```


## Coverage gap reporting

The Explorer collects line-level coverage alongside its edge bitmap. After exploration, it identifies branch statements (`if`, `for`, `while`, `try`, `except`, `assert`, `raise`, `match`) in target modules that were never executed, and emits structured gap data with `reachable()` suggestions.

These are branches the Explorer did not reach in N runs — not necessarily unreachable code. A longer run, different seed, or different swarm configuration might reach them. The `confidence` field makes this explicit: it is always `"not_reached"`, never `"unreachable"`.

### Output

```
Line coverage: 45/60 (75%)
Not reached in 200 runs: 3 branch(es) in target modules
  myapp/api.py:42 if response.status >= 500:
    add: reachable("myapp/api.py:42: if response.status >= 500:")
```

### API

```python
result = explorer.run(max_time=60)

for gap in result.coverage_gaps:
    print(f"{gap['file']}:{gap['line']} {gap['code']}")

for s in result.reachability_suggestions():
    print(s["suggestion"])    # reachable("myapp/api.py:42: ...")
    print(s["confidence"])    # "not_reached"
    print(s["runs"])          # 200
```

Adding the suggested `reachable()` calls to your code lets future exploration runs prove whether each branch is reachable or genuinely dead.


## Generating tests from exploration

!!! quote "What this unlocks"
    The Explorer can automatically write test functions for you. After it finishes exploring, it takes its best discoveries — the failures it found and the deepest code paths it reached — and turns each one into a standalone pytest test. You commit those tests, and now your CI permanently guards those deep paths. Your test suite grows smarter every time you run the Explorer.

The Explorer can turn its traces into standalone pytest test functions. Each generated test replays an exact sequence of rules and fault toggles -- the sequences the Explorer found most valuable during exploration.

### From the CLI

```bash
ordeal explore --generate-tests tests/test_generated.py
```

This explores normally, then writes a test file containing one test function per trace. The generated file looks like:

```python
"""Generated by ordeal explorer. Do not edit -- re-run ordeal explore to regenerate."""

from tests.test_chaos import MyServiceChaos


def test_fail_r42():
    """Run 42, AssertionError: balance went negative, 5 steps, 3 new edges"""
    machine = MyServiceChaos()
    try:
        # activate fault: timeout(api.call)
        for f in machine._faults:
            if f.name == 'timeout(api.call)': f.activate()
        machine.do_process(x=5, mode='strict')
        machine.do_retry()
        # deactivate fault: timeout(api.call)
        for f in machine._faults:
            if f.name == 'timeout(api.call)': f.deactivate()
        machine.do_check()
    finally:
        machine.teardown()
```

Each test is self-contained: it creates a machine, replays the exact sequence, and tears down. Fault toggles are included so the test exercises the same failure conditions.

### From Python

```python
from ordeal.trace import generate_tests

explorer = Explorer(MyServiceChaos, target_modules=["myapp"])
result = explorer.run(max_time=60, record_traces=True)

test_source = generate_tests(result.traces)
Path("tests/test_generated.py").write_text(test_source)
```

Note: `record_traces=True` is required to capture the traces. The `--generate-tests` CLI flag enables this automatically.

### What gets generated

The Explorer records traces for:

- **Failure traces**: sequences that triggered an exception. These become regression tests -- if you fix the bug, the test verifies it stays fixed.
- **Coverage traces**: sequences that discovered new code paths. These become coverage tests -- they exercise deep paths that random testing misses.

### When to use generated tests

- **After a deep exploration run**: explore for 30 minutes, generate tests, commit them. Your CI now permanently covers the deep paths the Explorer found.
- **After mutation-guided exploration**: the Explorer kills mutants by finding specific sequences. Generate tests from those sequences to lock in the mutation coverage.
- **As a starting point**: generated tests are not a replacement for hand-written ChaosTests. They are a supplement -- they cover specific deep paths while your ChaosTest defines the overall exploration space.

### Regenerating

Generated tests are tied to the current code. When you change your system, re-run the explorer to regenerate:

```bash
ordeal explore --generate-tests tests/test_generated.py
```

The file is overwritten each time. Do not edit it by hand -- your edits will be lost on the next run.


## CI integration

!!! quote "How to explore this"
    The Explorer fits naturally into your CI pipeline. You add a step that runs it, and if it finds bugs, the build fails — just like any other test. Failure traces are saved as artifacts so you can download and replay them locally. The pattern is: explore in CI, review the report, fix what it found, push again. Over time, you can also generate regression tests from the Explorer's discoveries so those deep paths stay covered permanently.

For CI, configure the Explorer with a longer time limit, JSON report output, and trace saving. The exit code is non-zero when failures are found, so it integrates with any CI system that checks exit codes.

### ordeal.toml for CI

```toml
[explorer]
target_modules = ["myapp"]
max_time = 300
seed = 0

[[tests]]
class = "tests.chaos.test_api:APIChaos"

[[tests]]
class = "tests.chaos.test_scoring:ScoringChaos"

[report]
format = "json"
output = "ordeal-report.json"
traces = true
traces_dir = ".ordeal/traces"
```

### GitHub Actions

```yaml
- name: Run ordeal explorer
  run: |
    uv run ordeal explore -v -c ordeal.toml --generate-tests tests/test_generated.py

- name: Run generated regression tests
  run: |
    uv run pytest tests/test_generated.py -v

- name: Upload failure traces
  if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: ordeal-traces
    path: .ordeal/traces/
    retention-days: 30

- name: Upload exploration report
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: ordeal-report
    path: ordeal-report.json
```

The `if: failure()` condition on the traces upload means artifacts are only saved when the Explorer finds failures. The report is always uploaded so you can review edge counts and run statistics even on passing builds.

### Failing the build

The `ordeal explore` command returns exit code 1 when any failure is found. No special configuration is needed -- CI will fail the step automatically. If you want to fail only on specific conditions, parse the JSON report in a subsequent step.


## Explorer vs Hypothesis

!!! quote "Think of it this way"
    Hypothesis and the Explorer are teammates, not competitors. Hypothesis is fast and lightweight — it runs during every CI push and catches the obvious bugs. The Explorer is slower but deeper — it remembers what it has seen, learns which directions look promising, and tunnels into corners that random testing would take years to reach. Use Hypothesis for everyday testing and the Explorer for periodic deep dives.

| | Hypothesis (`pytest --chaos`) | Explorer (`ordeal explore`) |
|---|---|---|
| **Guidance** | None. Random rule selection with Hypothesis's internal heuristics for diversity. | Edge coverage feedback. Biases exploration toward sequences that discover new code paths. |
| **Memory** | Stateless. Each test run is independent. | Checkpoints. Saves interesting states and branches from them. |
| **Scheduling** | Uniform random. | Energy-weighted. Productive checkpoints get more attention. |
| **Shrinking** | Algebraic. Hypothesis shrinks inputs and rule sequences using its built-in strategies. | Delta debugging on traces. Removes chunks, individual steps, and unnecessary faults. |
| **Speed** | Fast. No tracing overhead. Thousands of examples per second. | Slower. `sys.settrace` adds overhead proportional to code size in target modules. Hundreds to thousands of runs per second depending on complexity. |
| **Integration** | pytest. Runs alongside your other tests. | CLI and TOML. Separate from your test suite. Can also be called from Python. |
| **Best for** | Everyday CI testing. Fast feedback on rule interactions and basic fault injection. | Deep exploration. Pre-release validation. Finding bugs that require long, specific action sequences. |

**Recommendation: use both.**

Run `pytest --chaos` in your CI pipeline for every push. It catches the easy bugs quickly and gives fast feedback. Run `ordeal explore` on a schedule (nightly, or before releases) for deep exploration. The two complement each other -- Hypothesis covers breadth, the Explorer covers depth.


## Tuning tips

!!! quote "The key insight"
    Tuning the Explorer is about reading its output and adjusting one knob at a time. If edges stop climbing, you need to widen what it watches or let it run longer sequences. If it is too slow, narrow the focus. If failures are flaky, pin the seed. Each tip below matches a symptom to its fix. You do not need to understand all of this upfront — come back here when you see a specific problem.

**Edges plateau early?**
Add more `target_modules`. If you are only tracking `myapp.core` but many bugs live in `myapp.api` or `myapp.cache`, the Explorer cannot see edge transitions in those modules. Widen the target to give it more signal.

**No failures but low edge count?**
Increase `steps_per_run`. If your system needs 40 operations to reach an interesting state and `steps_per_run` is 50, the Explorer only has 10 steps to explore from there. Try 100 or 200.

**Too slow?**
Reduce `target_modules` to the core paths where you expect bugs. Tracing the entire application is expensive. Focus on the modules that handle the critical logic.

**Flaky failures?**
Use a fixed `seed`. If the same seed produces the failure reliably, it is a real bug. If the failure only appears with some seeds, it may depend on nondeterminism outside ordeal's control (system time, network, threading). Fix the nondeterminism source or add deterministic simulation (see `ordeal.simulate` for `Clock` and `FileSystem` fakes).

**Checkpoints full but edges still climbing?**
Increase `max_checkpoints`. The default of 256 is conservative. If your system has many distinct interesting states, a larger corpus (512-1024) lets the Explorer keep more of them alive.

**Explorer always branches from the same checkpoint?**
Switch `checkpoint_strategy` from `"energy"` to `"uniform"`. Energy scheduling can sometimes over-concentrate on one highly productive checkpoint. Uniform selection gives every checkpoint equal attention, which can break out of local optima.

**Want more fault activity?**
Increase `fault_toggle_prob` from 0.3 to 0.5. This means faults toggle more frequently, creating more interleaving between normal operations and fault conditions.

**Want deeper fault-free exploration?**
Decrease `fault_toggle_prob` to 0.1. The system runs longer without fault interference, building up more complex state before faults are injected.

## Parallel exploration

!!! quote "At any scale"
    One developer exploring on a laptop with 4 cores. A CI pipeline with 16 workers grinding through a pre-release check. A nightly job with 64 workers on a beefy machine. Same configuration, same `ordeal.toml`, same deterministic seeds — just more coverage per minute. The Explorer scales from "quick local check" to "deep organizational sweep" without changing how you use it.

!!! quote "In plain English"
    Parallel exploration means running multiple Explorers at the same time, each on its own CPU core. They automatically share their discoveries — when one worker finds a new code path, the others see it and skip that path. When one worker reaches a deep, interesting state, the others can pick up from that state and push even deeper. You do not need to configure anything special: by default it uses all your cores.

By default, `ordeal explore` uses all CPU cores (`workers = 0` means auto-detect). Each worker explores independently with a unique seed. Results are aggregated: runs and steps are summed, edges are unioned for the true unique count.

Workers collaborate through two mechanisms, both enabled by default:

**Shared edge bitmap** (`share_edges=True`): a 64KB shared-memory buffer where each byte marks a discovered edge hash. When worker 3 finds edge `0xAB12`, workers 1-8 see it immediately and skip it. Single-byte writes are atomic -- zero locks needed. This prevents workers from wasting time rediscovering edges another worker already found.

**Shared checkpoint pool** (`share_checkpoints=True`): when a worker discovers a significant new state (2+ new edges), it publishes the machine state as a pickle file to a shared temporary directory. Other workers poll this directory every 2 seconds and load new checkpoints into their local corpus with initial energy equal to the discovery reward. This means one worker can branch from a state another worker discovered -- the difference between "8 independent explorers" and "8 explorers collaborating on the same search."

Either mechanism can be disabled independently:

```python
explorer = Explorer(
    MyServiceChaos,
    target_modules=["myapp"],
    workers=4,
    share_edges=True,          # default: skip edges others found
    share_checkpoints=True,    # default: share interesting states
)
```

The pool is most valuable when the state space is deep (many steps to reach an interesting state). Worker A might reach phase 2 of your service after a lucky sequence. Without sharing, workers B-D must independently find the same sequence. With sharing, worker A publishes its phase-2 state and everyone branches from there.

### Usage

```bash
ordeal explore                          # auto: uses all CPU cores
ordeal explore -w 4                     # explicit: 4 workers
ordeal explore -w 1                     # sequential (no multiprocessing)
```

```python
explorer = Explorer(
    MyServiceChaos,
    target_modules=["myapp"],
    workers=0,   # 0 = auto (cpu_count), default
)
result = explorer.run(max_time=60)
```

```toml
[explorer]
target_modules = ["myapp"]
max_time = 300
workers = 0       # auto (default)
```

### Measuring scaling with `ordeal benchmark`

The `ordeal benchmark` CLI measures your actual sigma (contention) and kappa (coherence) by running exploration at N=1, 2, 4, 8... workers and fitting the Universal Scaling Law:

```bash
ordeal benchmark                          # fit USL from ordeal.toml
ordeal benchmark --max-workers 16         # test up to 16
ordeal benchmark --time 30                # 30s per trial
ordeal benchmark --metric steps           # fit on steps/sec
```

Example output:

```
Scaling Analysis (Universal Scaling Law)
  sigma (contention):  0.080755
  kappa (coherence):   0.005578
  Regime:              usl
  Optimal workers:     13.4
  Peak throughput:     7.64x

  Diagnosis:
    Contention (sigma): 8.1% serialized fraction.
    Coherence (kappa):  0.005578 cross-worker sync cost.
```

**Reading the results:**
- **sigma** is the fraction of work that's serialized (process management). Typically 4-10%.
- **kappa** is the cross-worker coordination cost (edge bitmap sync). Zero means fully independent.
- **Optimal workers** is where adding more starts hurting. Beyond this, kappa's quadratic cost outweighs throughput.
- **Regime**: `linear` (perfect), `amdahl` (bounded by sigma), or `usl` (bounded by both).

If `n_optimal` is low (< 4), your test has high contention. If it's high (> 16), you're scaling well.

### Thread safety

All shared state is lock-protected for free-threaded Python 3.13+ (no GIL). The parallel explorer uses multiprocessing (not threads), so each worker has its own interpreter. Tested and verified on CPython 3.13.5 free-threaded build.


---

**Related pages:**

- [Coverage Guidance](../concepts/coverage-guidance.md) -- the theory behind edge coverage, checkpointing, and energy scheduling
- [Configuration](configuration.md) -- full schema for `ordeal.toml` including `[[tests]]` and `[report]` sections
