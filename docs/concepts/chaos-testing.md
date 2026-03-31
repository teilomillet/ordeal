# Chaos Testing

!!! quote "In plain English"
    Chaos testing is a way to test your code by **breaking things on purpose**. Instead of writing one test for each bug you can imagine, you tell ordeal what can go wrong, what should always be true, and let it explore thousands of failure combinations automatically. It's like hiring an adversary whose only job is to find the thing you missed.

## The idea

Think about how you'd test a bridge. The obvious approach: park a car on it, check that it holds. That's a unit test. Then park a truck on it. Then two trucks. You're testing specific loads you thought of.

Now think about what actually destroys bridges. It's not one truck. It's a truck hitting a pothole during a windstorm while the temperature drops and the steel contracts. Multiple forces, hitting at the same time, in a combination nobody designed for.

Traditional tests are the car on the bridge. They verify scenarios you imagined. Chaos testing is the windstorm: you declare what forces exist, state what must survive, and let a machine explore every combination of forces, timings, and sequences until something breaks -- or until nothing does.

That's the core idea. You don't write test cases for failures. You describe the world your code lives in, define what "correct" means, and let an engine systematically explore the space of things that can go wrong.

## ChaosTest

!!! quote "Think of it this way"
    A `ChaosTest` is your **crash-test arena**. You describe three things: what can break (faults), what your code does (rules), and what must always be true (invariants). Then ordeal runs your code thousands of times, mixing operations and failures in every order it can think of. When something breaks, it hands you the shortest possible replay so you know exactly what went wrong.

    The class lives in `ordeal/chaos.py`. It handles the exploration, shrinking, and fault coordination — you just fill in the pieces.

`ChaosTest` is the base class for chaos tests in ordeal. It extends Hypothesis's `RuleBasedStateMachine`, which means it inherits a powerful exploration engine: Hypothesis generates random sequences of method calls, tracks state, and when something fails, it shrinks the sequence to the minimal reproduction.

A `ChaosTest` has three ingredients:

1. **Faults** -- a list of `Fault` objects that describe what can go wrong
2. **Rules** -- methods decorated with `@rule()` that represent system operations
3. **Invariants** -- methods decorated with `@invariant()` that must hold after every step

Here's a complete example. Suppose you have a data pipeline that fetches records from an API and scores them with a model:

```python
import math
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical

class PipelineChaos(ChaosTest):
    faults = [
        timing.timeout("pipeline.api.fetch"),
        numerical.nan_injection("pipeline.model.score"),
        timing.intermittent_crash("pipeline.cache.write", every_n=5),
    ]

    def __init__(self):
        super().__init__()
        self.pipeline = Pipeline()

    @rule()
    def fetch_and_score(self):
        try:
            result = self.pipeline.process_next()
        except TimeoutError:
            return  # timeouts are expected, system must handle them
        always(not math.isnan(result.score), "score is never NaN")
        always(result.score >= 0, "score is non-negative")

    @rule()
    def flush_cache(self):
        self.pipeline.flush()

    @invariant()
    def data_is_consistent(self):
        """Must hold after every single step, including fault toggles."""
        for record in self.pipeline.committed_records():
            assert not math.isnan(record.score), f"NaN in committed record {record.id}"

# This line makes pytest discover and run the chaos test
TestPipelineChaos = PipelineChaos.TestCase
```

Three faults. Two rules. One invariant. From these ingredients, Hypothesis explores thousands of sequences: fetch, flush, fetch, fetch, flush -- with faults toggling on and off at different points in the sequence. If any sequence violates an invariant or assertion, Hypothesis shrinks it to the shortest reproduction.

You wrote 30 lines. The machine explores a space you couldn't cover with 300 hand-written tests.

### Why RuleBasedStateMachine

!!! quote "Why this matters"
    You don't have to learn a new testing framework from scratch. If you've ever used Hypothesis, you already know 90% of how ordeal works. Rules, invariants, bundles, preconditions -- they all work exactly the same way. Ordeal just adds the concept of **faults** (things that break) and a **nemesis** (the thing that decides when to break them). Everything else is Hypothesis under the hood.

Hypothesis's `RuleBasedStateMachine` was designed for exactly this kind of testing: explore sequences of operations on a stateful system, check properties after each step, and shrink failures to minimal examples. Ordeal doesn't reinvent this. It extends it by adding the concept of faults -- things that go wrong -- and a nemesis that controls them.

Everything Hypothesis provides works inside a `ChaosTest`: `@rule()`, `@invariant()`, `@initialize()`, `@precondition()`, `Bundle` for passing state between rules. If you know Hypothesis stateful testing, you already know how `ChaosTest` works. The only new thing is the faults list and the nemesis.

## The nemesis

!!! quote "The key insight"
    The nemesis is your code's **personal adversary**. It's an invisible rule that ordeal adds to every chaos test -- you never write it, it just exists. Each time it runs, it flips a fault on or off. Because it's mixed in with your own rules, failures happen *during* your operations, not before or after. That's what makes it realistic: in production, things break mid-operation, not at convenient moments.

The nemesis is the most important idea in ordeal. It comes from [Jepsen](https://jepsen.io), Kyle Kingsbury's framework for testing distributed systems. In Jepsen, a "nemesis" is an adversary process that injects failures -- network partitions, node crashes, clock skew -- while the system is running. The insight: real failures don't wait for a convenient moment. They happen *during* operations.

In ordeal, the nemesis is an auto-injected rule. You never write it. It exists in every `ChaosTest` automatically. Here's what it does, simplified:

```python
@rule(data=st.data())
def _nemesis(self, data):
    if not self._faults:
        return
    fault = data.draw(st.sampled_from(self._faults))
    if fault.active:
        fault.deactivate()
    else:
        fault.activate()
```

Each time the nemesis executes, it picks one fault at random and toggles it. If the fault was off, it turns on. If it was on, it turns off.

The critical insight: **the nemesis is just another Hypothesis rule**. It sits alongside your application rules in the same state machine. Hypothesis doesn't know or care that it's special -- it just sees another rule to interleave into the sequence. This means:

- Hypothesis explores *when* faults activate relative to your operations
- Hypothesis explores *which* faults are active at each point
- Hypothesis explores *combinations* of active faults
- When a failure is found, Hypothesis shrinks the nemesis calls too -- finding the minimal fault schedule that triggers the bug

The adversary isn't external. It's not a separate process or a pre-scripted sequence. It's part of the state machine itself, subject to the same exploration and shrinking as everything else.

### Why this matters

Consider the alternative: you could write tests that manually activate faults before calling your code. Something like:

```python
def test_timeout_during_fetch():
    timeout_fault.activate()
    result = pipeline.process_next()
    timeout_fault.deactivate()
    assert pipeline.is_consistent()
```

This tests one scenario: timeout is active during one fetch. But what about: timeout activates, two fetches happen, then NaN injection activates, then a flush, then timeout deactivates? That's a five-step sequence you'd have to write by hand. And there are thousands of such sequences.

With the nemesis, you don't write any of them. Hypothesis generates them. And when it finds one that breaks, it tells you the shortest version:

```
state = PipelineChaos()
state._nemesis(data=...)       # activates nan_injection
state.fetch_and_score()        # NaN propagates to committed record
state.data_is_consistent()     # invariant violated
```

Three steps. Minimal. Actionable.

## Swarm mode

!!! quote "What this unlocks"
    Swarm mode makes ordeal **smarter about which faults to combine**. Instead of turning everything on at once (which just floods your code with errors), each test run picks a random subset of faults. Over hundreds of runs, ordeal covers far more interesting combinations than brute force ever could. And when a failure happens, it tells you exactly which faults were necessary to trigger it -- not just "everything was broken."

Swarm mode solves a subtle problem with fault injection.

When all faults are active at the same time, your code spends most of its time in error-handling paths. Timeouts fire, NaN injection corrupts data, crashes happen -- and every test run exercises the same heavily-faulted execution. The error handlers dominate. The interesting code paths -- the ones where *some* things work and *some* things fail -- are never explored.

Think of it like a fire drill. If you set every room on fire at once, everyone runs for the exits and you learn exactly one thing: the exits work. If you set *one* room on fire, you learn how the alarm propagates, whether the sprinklers engage, whether people in adjacent rooms react correctly. Different single rooms on fire teach you different things. The aggregate coverage of many partial fires is much higher than one total fire.

That's swarm testing. Instead of activating all faults every run, each run activates a random *subset* of faults. Over many runs, this covers more fault combinations than always-all-on.

Enable it by setting `swarm = True`:

```python
class PaymentServiceChaos(ChaosTest):
    faults = [
        timing.timeout("payments.gateway.charge"),
        timing.slow("payments.gateway.refund", delay=5.0),
        numerical.nan_injection("payments.fees.calculate"),
        timing.intermittent_crash("payments.audit.log", every_n=3),
    ]
    swarm = True  # each run uses a random subset of faults

    @rule()
    def charge(self):
        try:
            self.service.charge(amount=100)
        except TimeoutError:
            pass
        always(self.service.balance_is_correct(), "balance consistent")

    @rule()
    def refund(self):
        self.service.refund(amount=50)

    @invariant()
    def audit_trail_valid(self):
        assert self.service.audit_trail_is_complete()

TestPaymentServiceChaos = PaymentServiceChaos.TestCase
```

With four faults, there are 15 non-empty subsets (2^4 - 1). Each test run draws one. Over 200 runs (Hypothesis's default), that's roughly 13 runs per subset -- each exploring different rule orderings within that fault configuration.

### How swarm selection works

At the start of each test case, before any rules execute, ordeal runs an `@initialize` step that asks Hypothesis to draw a boolean for each fault: include it or exclude it. The constraint is that at least one fault must be included (a run with zero faults isn't chaos testing).

```python
@initialize(data=st.data())
def _swarm_init(self, data):
    if not self.__class__.swarm or len(self._faults) <= 1:
        return
    mask = data.draw(
        st.lists(
            st.booleans(),
            min_size=len(self._faults),
            max_size=len(self._faults),
        ).filter(any),  # at least one fault
    )
    self._faults = [f for f, keep in zip(self._faults, mask) if keep]
```

Because the booleans are drawn by Hypothesis, they participate in shrinking. If a failure requires a specific combination of faults, Hypothesis will shrink the mask to the minimal set of `True` values that still reproduces the failure. You don't just learn *that* something failed -- you learn *which faults were necessary* to trigger it.

### When to use swarm mode

Use swarm mode when you have three or more faults. With one or two faults, the space of subsets is small enough that the nemesis alone covers it. With five or ten faults, the combination space explodes and swarm mode becomes essential for coverage.

| Faults | Subsets (2^n - 1) | Swarm recommended |
|--------|-------------------|-------------------|
| 1-2    | 1-3               | No                |
| 3-4    | 7-15              | Yes               |
| 5+     | 31+               | Strongly yes      |

## How a run works, step by step

!!! quote "How to explore this"
    Don't worry about memorizing these steps. The loop is simple: **initialize, run a mix of your rules and the nemesis, check invariants after every step, clean up**. If anything breaks, ordeal rewinds and finds the shortest path to the failure. Think of it like a video game replay — ordeal records every move, then edits out everything that wasn't necessary to trigger the bug.

Here's exactly what happens when Hypothesis executes one test case of a `ChaosTest`:

**Step 1: Initialization.** Ordeal copies the fault list and resets all faults to inactive. If swarm mode is on, it draws the fault subset.

**Step 2: Rule sequence generation.** Hypothesis generates a sequence of rule calls. The available rules are your application rules plus the nemesis. Hypothesis picks from them with equal probability (by default). A typical sequence might look like:

```
_nemesis -> fetch_and_score -> fetch_and_score -> _nemesis -> flush_cache -> _nemesis -> fetch_and_score
```

**Step 3: Execution with invariant checks.** For each rule in the sequence:

1. The rule executes. If it's the nemesis, a fault toggles. If it's your rule, your code runs (with whatever faults are currently active).
2. All `@invariant()` methods execute. If any raises, the test fails.
3. Any `always()` call inside the rule that receives `False` raises immediately, failing the test.

**Step 4: Teardown.** All faults are deactivated and reset, regardless of which were active.

**Step 5: Shrinking (on failure).** If any step failed, Hypothesis reruns the test with progressively shorter sequences, removing rules that aren't needed to reproduce the failure. It also shrinks the nemesis's random choices and the swarm mask. The result is the minimal sequence of steps that triggers the bug.

Here's a diagram of one execution:

```
  INITIALIZE          STEP 1         STEP 2         STEP 3         STEP 4         TEARDOWN
  ──────────         ────────       ────────       ────────       ────────        ──────────
  faults: all OFF    _nemesis       fetch_and_     _nemesis       fetch_and_      faults: all OFF
  swarm: pick        toggles ON     score()        toggles ON     score()
  subset             timeout        runs OK        nan_inject.    NaN detected!
                                                                  always() fails
                     invariant()    invariant()    invariant()
                     PASS           PASS           PASS

  Hypothesis shrinks: removes step 2, finds 3-step reproduction.
```

## When to use chaos testing

!!! quote "What you can do with this"
    If your code talks to APIs, databases, file systems, or ML models -- anything that can fail, lag, or return garbage -- chaos testing is built for you. You don't need a distributed system or a huge codebase. Even a small service with two API calls and a cache is enough to start. The faults you need are already in `ordeal/faults/` -- timing issues, I/O errors, and numerical corruption, ready to use.

Chaos testing is most valuable when your system is **stateful** and **depends on things that fail**.

**Strong fit:**

- Services that call APIs, databases, or other services. These dependencies time out, return errors, and return garbage. Your error handling needs to work in combination, not just one failure at a time.
- Data pipelines that process, transform, and store records. Partial failures (some records succeed, some fail) create subtle corruption that unit tests never catch.
- ML inference pipelines where models can return NaN, Inf, or wrong-shaped tensors. Numerical faults propagate silently and corrupt downstream results.
- Anything with retries, caches, or fallback logic. These mechanisms interact in complex ways under failure. A retry during a cache flush during a partial timeout -- that's where the bugs live.

**Weaker fit:**

- Pure functions with no state and no dependencies. Use [property-based testing](https://hypothesis.readthedocs.io/) directly -- no faults to inject.
- One-shot scripts that run and exit. Chaos testing shines with sequences of operations, not single calls.

The rule of thumb: if your system can be in different states and bad things can happen to its dependencies, chaos testing will find bugs that nothing else will.

## Putting it all together

!!! quote "The big picture"
    Everything you've read on this page -- faults, nemesis, swarm mode, invariants, assertions -- comes together in a single class, usually under 50 lines. You declare what can go wrong, what your code does, and what must stay true. Ordeal handles the rest: exploring thousands of scenarios, finding failures, and shrinking them to the smallest possible reproduction. One class replaces dozens of hand-written tests.

Here's a realistic example that uses everything: faults, nemesis, swarm mode, invariants, and property assertions.

```python
import math
from ordeal import ChaosTest, rule, invariant, always, sometimes
from ordeal.faults import timing, numerical, io

class InventoryServiceChaos(ChaosTest):
    """Chaos test for an inventory management service.

    The service tracks stock levels, processes orders, and syncs
    with an external warehouse API. Three things can go wrong:
    the warehouse API times out, the pricing model returns NaN,
    and the database write crashes intermittently.
    """

    faults = [
        timing.timeout("inventory.warehouse.sync"),
        numerical.nan_injection("inventory.pricing.calculate"),
        timing.intermittent_crash("inventory.db.write", every_n=4),
        io.disk_full("inventory.export.save"),
    ]
    swarm = True  # explore fault subsets across runs

    def __init__(self):
        super().__init__()
        self.service = InventoryService()
        self.expected_stock = {}  # shadow state for verification

    @rule()
    def add_stock(self):
        self.service.add("widget", quantity=10)
        self.expected_stock["widget"] = self.expected_stock.get("widget", 0) + 10

    @rule()
    def process_order(self):
        try:
            self.service.order("widget", quantity=1)
        except (TimeoutError, RuntimeError):
            return  # failures are expected -- but state must stay consistent
        if "widget" in self.expected_stock and self.expected_stock["widget"] > 0:
            self.expected_stock["widget"] -= 1

    @rule()
    def sync_warehouse(self):
        try:
            self.service.sync()
        except TimeoutError:
            pass

    @invariant()
    def stock_never_negative(self):
        for item, qty in self.service.stock_levels().items():
            assert qty >= 0, f"{item} has negative stock: {qty}"

    @invariant()
    def no_nan_in_prices(self):
        for item, price in self.service.current_prices().items():
            assert not math.isnan(price), f"{item} has NaN price"

    @rule()
    def check_coverage(self):
        """Verify we're exercising both success and failure paths."""
        sometimes(len(self.service.successful_syncs) > 0, "syncs sometimes succeed")
        sometimes(len(self.service.failed_syncs) > 0, "syncs sometimes fail")

TestInventoryServiceChaos = InventoryServiceChaos.TestCase
```

Four faults, swarm mode, three rules, two invariants, two `sometimes` assertions. This single class generates more meaningful test coverage than dozens of hand-written test cases, and when it finds a failure, it hands you the minimal reproduction.

!!! quote "You're ready"
    You now understand how chaos testing works — faults, rules, invariants, the nemesis, and swarm mode. That's everything you need to write your first chaos test. Go to [Writing Tests](../guides/writing-tests.md) for patterns you can copy and adapt to your system. Or run `ordeal mine` on your code and let ordeal write the first test for you.

## What's next

- **[Property Assertions](property-assertions.md)** -- how `always`, `sometimes`, `reachable`, and `unreachable` work, and when to use each one
- **[Fault Injection](fault-injection.md)** -- PatchFault, LambdaFault, and the built-in fault libraries for timing, I/O, and numerical failures
- **[Coverage Guidance](coverage-guidance.md)** -- how `ordeal explore` uses AFL-style edge coverage to systematically find bugs
