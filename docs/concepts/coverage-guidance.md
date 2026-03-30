# Coverage Guidance

How ordeal's Explorer uses code coverage to find bugs that random testing cannot.

---

## The problem with random exploration

Imagine you are testing a service that has 20 rules and 5 possible faults. Each run picks random rules and toggles random faults. Most runs will exercise the common, well-traveled paths -- the same handful of state transitions your code hits every day.

The bugs do not live there. They live in the rare states: the one where a timeout fires right after a retry, during a batch write, while the cache is cold. That state might require a specific sequence of 8 actions to reach. A random explorer that picks actions uniformly will need millions of runs to stumble into it by accident.

This is not a theoretical problem. It is the reason random testing plateaus. After a few thousand runs, you stop finding new things. The coverage curve goes flat. The bugs that remain are hiding behind combinations that random selection will not reach in any reasonable time.

You need guidance.

## Edge coverage (AFL-style)

ordeal tracks **edges**, not lines.

An edge is a transition from one code location to another. When your program executes line 42 and then line 87, that is one edge. When it executes line 42 and then line 15 (because a condition was false this time), that is a different edge. Same starting point, different destination, different edge.

The implementation uses `sys.settrace` to intercept every line event in your target modules. Each location is hashed to a 16-bit value:

```
loc = hash((filename, lineno)) & 0xFFFF
edge = prev_loc XOR loc
prev_loc = loc >> 1
```

This is the same hashing scheme AFL uses. The XOR captures directionality -- going from A to B produces a different edge than going from B to A. The right-shift of `prev_loc` ensures that a loop body (A to A) does not hash to zero.

**Analogy.** Think of your code as a city map. Lines are streets. Edges are intersections -- the specific turn you made to get from one street to another. If you only count which streets you drove on, you miss the structure of the route. Two completely different routes might visit the same streets but take different turns. Edge coverage sees the difference. Line coverage does not.

The `CoverageCollector` only traces files that match your `target_modules`. If you set `target_modules = ["myapp"]`, it will track edges inside `myapp/` but ignore Hypothesis internals, standard library code, and third-party packages. This keeps overhead focused and the edge set meaningful.

## Checkpoints

When a run discovers edges that have never been seen before, ordeal saves a **checkpoint**: a deep copy of the entire ChaosTest machine at that moment -- all state variables, all active faults, the full history.

Future runs can start FROM that checkpoint instead of from scratch. This means the Explorer does not have to re-discover the rare state every time. It saves its progress and branches forward into unknown territory.

**Analogy.** You are exploring a cave system. Without checkpoints, every expedition starts from the cave entrance. You spend most of your time walking through well-mapped tunnels to reach the frontier. With checkpoints, you leave a camp at the deepest point you have reached. Next expedition, you start from camp and push further.

This is what makes the Explorer qualitatively different from random testing. Random testing is memoryless. The Explorer has memory.

A checkpoint stores:

- **machine_copy**: A full deep copy of the ChaosTest instance (via `copy.deepcopy`)
- **new_edge_count**: How many new edges this checkpoint discovered
- **step / run_id**: When and where it was created
- **energy**: A scheduling weight (explained next)

## Energy scheduling

Not all checkpoints are equally promising. A checkpoint that led to 15 new edges is more interesting than one that led to 1. A checkpoint that has been explored 50 times without finding anything new is less interesting than a fresh one.

ordeal uses an energy system to express this:

**When new edges are found** from a checkpoint:

```
energy += new_edges * 2.0
```

**When no new edges are found** from a checkpoint:

```
energy = max(0.01, energy * 0.95)
```

The constants are: reward factor = 2.0, decay factor = 0.95, minimum energy = 0.01.

The decay is slow -- 0.95 means it takes about 14 barren runs to halve a checkpoint's energy. This is deliberate. A checkpoint that was productive once might be productive again from a different angle. You do not want to abandon it too quickly. But you do want to gradually shift attention toward checkpoints that keep delivering.

Selection is weighted by energy. A checkpoint with energy 10.0 is ten times more likely to be selected than one with energy 1.0.

**Analogy.** Think of GPS route suggestions. A road that led you to a new neighborhood gets a higher priority. A road you have driven twenty times without finding anything new gradually drops in the ranking. But it never drops to zero -- there might still be a side street you missed.

ordeal offers three checkpoint selection strategies:

| Strategy | How it picks | When to use |
|---|---|---|
| `"energy"` (default) | Weighted by energy score | General use. Best for most codebases. |
| `"uniform"` | All checkpoints equally likely | When you want exhaustive breadth. |
| `"recent"` | Newer checkpoints preferred | When the system has deep sequential states. |

When the checkpoint corpus reaches capacity (`max_checkpoints`, default 256), the lowest-energy checkpoint is evicted. The corpus is always full of the most promising states.

## The exploration loop

Here is the full loop, step by step.

```
                        +------------------+
                        |   Start new run  |
                        +--------+---------+
                                 |
                    +------------v-------------+
                    | Coin flip (40% default): |
                    | Load checkpoint or       |
                    | start fresh?             |
                    +---+------------------+---+
                        |                  |
                  [checkpoint]          [fresh]
                        |                  |
               +--------v-------+  +-------v--------+
               | deepcopy saved |  | new ChaosTest  |
               | machine state  |  | instance       |
               +--------+-------+  +-------+--------+
                        |                  |
                        +--------+---------+
                                 |
                        +--------v---------+
                   +--->| Step (1..N)      |
                   |    +--------+---------+
                   |             |
                   |    +--------v---------+
                   |    | 30% chance:      |
                   |    | toggle a fault   |
                   |    | 70% chance:      |
                   |    | execute a rule   |
                   |    +--------+---------+
                   |             |
                   |    +--------v---------+
                   |    | Check invariants |
                   |    +--------+---------+
                   |             |
                   |    +--------v---------+
                   |    | Collect coverage |
                   |    | snapshot         |
                   |    +--------+---------+
                   |             |
                   |       +-----v------+     +------------------+
                   |       | New edges? +---->| YES: save        |
                   |       +-----+------+     | checkpoint,      |
                   |             |             | boost energy     |
                   |          [no]             +------------------+
                   |             |
                   |    +--------v---------+
                   |    | More steps?      |
                   +----+ YES              |
                        |                  |
                        +--------+---------+
                                 | NO
                        +--------v---------+
                        | Decay energy of  |
                        | source checkpoint|
                        | (if came from    |
                        |  checkpoint)     |
                        +--------+---------+
                                 |
                        +--------v---------+
                        | Exception?       |
                        | Record failure   |
                        | with full trace  |
                        +--------+---------+
                                 |
                        +--------v---------+
                        | Time/runs left?  |
                        | YES: loop        |
                        | NO: shrink       |
                        |     failures,    |
                        |     return       |
                        |     results      |
                        +------------------+
```

In detail:

1. **Start or resume.** The Explorer flips a weighted coin (default: 40% chance of loading a checkpoint). If it loads a checkpoint, it deep-copies the saved machine state so the original is preserved for future runs. If not, it creates a fresh ChaosTest instance.

2. **Step loop.** For a random number of steps (1 to `steps_per_run`, default 50), the Explorer does one of two things:
   - With probability `fault_toggle_prob` (default 0.3): toggle a random fault on or off.
   - Otherwise: pick a random rule and execute it, drawing parameters from Hypothesis strategies.

3. **Invariant check.** After every step, all `@invariant()` methods run. If one fails, that is a bug.

4. **Coverage snapshot.** After every step, the Explorer takes a snapshot of the edge set. If there are edges not in the global set, they are new. New edges trigger a checkpoint save.

5. **Energy update.** After the run completes, the source checkpoint (if any) gets its energy updated. New edges boost it. No new edges decay it.

6. **Failure recording.** If an exception escapes, the Explorer records a `Failure` with the full trace: every rule called, every fault toggled, every parameter drawn. This trace is enough to reproduce the failure.

7. **Termination and shrinking.** When time or run count is exhausted, the Explorer shrinks each failure trace to its minimal reproducing sequence.

## Why this works

Most real bugs live at **feature intersections** -- two features interacting in a way nobody anticipated. A timeout during a write. A NaN flowing through a cache update. A retry loop hitting a closed connection.

These intersections produce new edges because the code follows a path it has not followed before. The timeout handler runs for the first time during a write, or the NaN propagates through a branch that usually gets normal floats. These are new control-flow transitions -- new edges.

Coverage guidance pushes exploration toward these intersections automatically. The Explorer does not need to know what your features are or how they interact. It sees that "this sequence of actions produced new code paths" and saves it. Then it explores variations of that sequence, looking for more new paths. The edge coverage is the signal. Energy scheduling makes the search efficient.

This is why coverage-guided testing finds bugs that random testing does not. Random testing treats all directions equally. Coverage-guided testing allocates more runs to directions that are producing new information. Over time, it tunnels into the deep corners of your state space -- exactly where the bugs are.

## Comparison with pure Hypothesis

Hypothesis is excellent at what it does: exploring rule interleavings with algebraic shrinking. When you write a `ChaosTest` and run it with pytest, Hypothesis explores different orderings of your rules and faults, and when it finds a failure, it shrinks it to a minimal example.

But Hypothesis has no coverage feedback. It does not know whether a particular sequence of rules exercised new code paths. It explores blindly -- guided only by randomness and its own internal heuristics for diversity.

The Explorer adds the coverage layer:

| | Hypothesis | Explorer |
|---|---|---|
| **Guidance** | None (random with heuristics) | Edge coverage feedback |
| **Memory** | None (each run is independent) | Checkpoints save interesting states |
| **Scheduling** | Uniform | Energy-weighted toward productive directions |
| **Best for** | Everyday property testing in CI | Deep exploration of interaction bugs |
| **Speed** | Fast (no tracing overhead) | Slower (sys.settrace has cost) |
| **Shrinking** | Algebraic (Hypothesis internals) | Delta debugging on traces |

The practical difference: Hypothesis might need 100,000 runs to find a bug that requires a specific 8-step sequence. The Explorer finds it in 500 runs because it checkpoints at step 4 (when it first hits a new edge) and then only needs to find the remaining 4 steps from there.

**Use both.** Run `ChaosTest` with pytest in CI for fast, broad coverage. Run the Explorer when you want to go deep -- before releases, after major refactors, or when you suspect there are interaction bugs that CI has not caught.

---

**Next steps:**

- [Shrinking](shrinking.md) -- how ordeal minimizes failure traces to the smallest reproducing case
- [Chaos Testing](chaos-testing.md) -- how ChaosTest, faults, and the nemesis work together
- [Explorer Guide](../guides/explorer.md) -- practical usage: CLI, configuration, interpreting results
