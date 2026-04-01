---
description: >-
  Property assertions for Python testing: always, sometimes, reachable,
  unreachable. The Antithesis assertion model for chaos testing with
  ordeal.
---

# Property Assertions

!!! quote "In plain English"
    A regular `assert` checks one thing at one moment. But what if you could make promises about *all* runs, *all* scenarios, *all* the chaos? That's what property assertions do. You get four simple tools that let you say "this must always be true," "this should happen at least once," "this code must run," or "this code must never run." Together, they cover every kind of correctness guarantee you'd ever want to make. All four live in `ordeal/assertions.py`.

## The problem with `assert`

You know how `assert` works. You check something right now, at this exact moment:

```python
assert score >= 0
```

If it's true, nothing happens. If it's false, the test fails. Simple.

But here's the limitation: `assert` can only talk about *this moment*. It checks one value, one time, and moves on. In chaos testing, you need to say things that span across time:

- "This should be true **every single time** it's evaluated, across thousands of runs."
- "This should happen **at least once** across all runs."
- "This code path should **never execute**, no matter what sequence of faults we throw at the system."

Regular `assert` can't express these ideas. If you write `assert cache_hits > 0` and the cache hasn't been hit *yet*, the test fails prematurely. If you write `assert not math.isnan(score)` and it passes this time, you have no guarantee it won't be NaN next time when a different fault is active.

You need a way to describe properties that hold *across time*, not just at a single point. Properties that accumulate evidence over a whole session and render a verdict at the end.

That's what ordeal's four assertion types do.


## The four types

!!! quote "Think of it this way"
    You have four tools in your toolkit, and each one asks a different question about your code. Two of them are strict guards that sound an alarm the instant something goes wrong. The other two are patient observers that watch an entire test session and only speak up at the end. Together, they let you describe exactly what "correct" means for your system.

All four are imported from the top level:

```python
from ordeal import always, sometimes, reachable, unreachable
```

Each takes a name string that identifies the property. The name is how the tracker follows it across runs, and what shows up in reports.


### `always(condition, name)` -- this must be true every time

!!! quote "Why this matters"
    `always` is ordeal's strictest assertion. It says: "No matter what faults are active, no matter what sequence of operations runs, this condition must hold every single time." If it ever fails, even once out of ten thousand checks, ordeal catches it immediately and shows you the shortest path to reproduce the problem.

```python
always(not math.isnan(result), "score is never NaN")
always(0 <= result <= 1, "score in valid range")
```

Think of `always` like a fire alarm. A fire alarm doesn't check once a day -- it monitors *continuously*. The instant smoke appears, it goes off. One violation is enough to trigger it, no matter how many times it was fine before.

`always` evaluates the condition every time the line executes. If the condition is `True`, the property records a pass and moves on. If the condition is `False` -- even once, even after a thousand passing evaluations -- it raises `AssertionError` immediately.

That immediate failure is important. When `always` fires inside a `ChaosTest`, Hypothesis catches the error and begins *shrinking*: it searches for the shortest sequence of steps that reproduces the violation. Instead of "the score was NaN somewhere during a 200-step run," you get "activate NaN injection, call score_user -- that's it, two steps."

**Use for:** Safety properties. Invariants. Anything where a single violation is unacceptable. "Data is never corrupted." "The balance is never negative." "The response is always valid JSON."


### `sometimes(condition, name)` -- this must be true at least once

!!! quote "What you can do with this"
    `sometimes` lets you verify that good things actually happen. Maybe your cache warms up. Maybe your retry logic eventually succeeds. Maybe a high score is possible. You don't need it to be true every time -- you just need proof that it's possible. If it never happens across an entire test session, ordeal tells you something is broken or unreachable.

```python
sometimes(cache.hit_count > 0, "cache hit happens at least once")
sometimes(score > 0.9, "high scores exist")
```

Think of `sometimes` like a smoke detector test button. You don't press it every second. But across the year, you need to press it at least once and see it work. If you never tested it, you have no evidence it works at all.

`sometimes` records every evaluation, but it never raises immediately. The condition can be `False` a hundred times and that's fine. What matters is that it's `True` at least once across the entire session. After all tests finish, the `PropertyTracker` checks: did this property ever pass? If not, something is wrong -- a code path is dead, a condition is unreachable, a feature is broken.

This is how you test *liveness* -- the property that something eventually happens. "The retry logic eventually succeeds." "The cache eventually warms up." "At least one test exercises the error handling path."

`sometimes` also has an immediate mode for standalone use, when you need to poll a condition:

```python
sometimes(lambda: cache.hit_rate() > 0, "cache warms up", attempts=100)
```

With `attempts`, it calls the function up to that many times and succeeds on the first `True`. If it's never true, it raises `AssertionError` immediately. This is useful outside of `ChaosTest`, when you want retry semantics without writing a loop.

**Use for:** Liveness properties. Reachability checks. "This feature is exercised at least once." "The optimizer eventually improves the score." "Error recovery actually runs."


### `reachable(name)` -- this code path must execute

!!! quote "The key insight"
    You wrote error handling code. But does it actually run? Many codebases have error handlers that *look* correct but never execute under test because the conditions that trigger them never arise. `reachable` is a one-line way to verify: "Yes, this code path really does get exercised." Drop it into any branch you care about and ordeal will tell you at the end of the session whether anything actually reached it.

```python
def handle_timeout(self):
    reachable("timeout handler runs")
    self.retry()
```

`reachable` is simpler than `sometimes`. There's no condition to evaluate. The mere act of calling `reachable("name")` records a hit. If the line executes at least once during the session, the property passes. If the line never executes, the property fails.

Think of it like a trip wire. You place it in a code path, and after the session you check: did anything cross the wire?

This catches a subtle class of bugs: dead code that looks alive. You wrote an error handler, a fallback path, a retry loop. Your code *looks* robust. But under test, with faults active, the path never actually executes -- maybe the fault doesn't trigger the right exception, maybe a guard clause catches it first, maybe the code is unreachable due to a logic error.

`reachable` makes the implicit explicit. Instead of hoping that your error handling runs, you *verify* it.

**Use for:** Verifying that error handlers execute. Confirming fallback paths are actually reachable. Ensuring that your fault injection is triggering the intended recovery logic.


### `unreachable(name)` -- this code path must never execute

!!! quote "What this unlocks"
    `unreachable` catches the scariest bugs: the ones that happen silently. Place it in code paths that should be impossible -- corrupted data, invalid states, branches that "can never happen." If chaos testing manages to reach one of those paths, ordeal fires immediately and gives you the minimal steps to reproduce. It turns invisible corruption into a loud, actionable failure.

```python
def process(self, data):
    if data.checksum != compute_checksum(data.payload):
        unreachable("data corruption detected")
    ...
```

`unreachable` is the opposite of `reachable`. If the line executes, even once, it raises `AssertionError` immediately.

Think of it like an alarm on a door that should never be opened. The moment someone opens it, the alarm goes off. There's no "it opened 3 times but that's fine" -- any single execution is a failure.

Like `always`, the immediate failure triggers Hypothesis shrinking, so you get the minimal reproducing sequence.

**Use for:** Impossible states. Silent corruption detection. "This branch should never be taken." "This data should never be invalid at this point." "If we reach this line, something has gone fundamentally wrong."


## Immediate vs. deferred

!!! quote "In plain English"
    Some promises must hold at every single moment -- if they ever break, that's a bug and you want to know right now. Other promises just need to come true at least once across the whole test session -- you can't judge them until the session is over. This is the difference between safety ("nothing bad ever happens") and liveness ("something good eventually happens"). Ordeal handles both automatically.

The four assertions split into two categories based on when they fail:

| Type | When it fails | Why |
|---|---|---|
| `always` | Immediately, on the first `False` | One violation is a bug. Fail fast, shrink fast. |
| `unreachable` | Immediately, on the first execution | Any execution is a bug. Fail fast, shrink fast. |
| `sometimes` | At session end, if never `True` | Needs the full session to accumulate evidence. |
| `reachable` | At session end, if never executed | Needs the full session to accumulate evidence. |

This isn't an arbitrary split. It follows from the semantics.

`always` and `unreachable` express *safety* properties: "something bad never happens." A single counterexample is proof of failure. There's no reason to wait -- fail immediately and let Hypothesis find the minimal reproduction.

`sometimes` and `reachable` express *liveness* properties: "something good eventually happens." You can't conclude failure from a single observation. You need to see the whole session before you can say "this never happened." So these are deferred -- the `PropertyTracker` collects evidence across all runs and checks at the end.


## The PropertyTracker

!!! quote "How to think about this"
    Ordeal keeps track of every assertion call automatically. Every time you call `always`, `sometimes`, `reachable`, or `unreachable`, ordeal writes it down. At the end of your test session, it reviews everything it recorded and gives you a clear report: which properties held, which ones failed, and how many times each was checked. You don't manage it yourself — it works behind the scenes.

Behind the scenes, every assertion call records data in a global `PropertyTracker`. This is a thread-safe singleton that accumulates results across the entire test session.

Each property is tracked by its name string and stores:

- **type** -- always, sometimes, reachable, or unreachable
- **hits** -- how many times the assertion was evaluated
- **passes** -- how many times the condition was `True` (for always/sometimes)
- **failures** -- how many times the condition was `False` (for always/sometimes)

The tracker has two states:

**Active** -- records everything. Assertions behave as described above. Activated by the `--chaos` flag or by calling `auto_configure()` in your `conftest.py`.

**Inactive** -- all four assertion functions are no-ops with negligible overhead. No recording, no checking. This is the default state, and it's why you can leave `always`/`sometimes`/`reachable`/`unreachable` calls in your production code safely. They're dormant until you turn chaos mode on.

```python
# conftest.py -- option A: use the CLI flag
# pytest --chaos

# conftest.py -- option B: activate programmatically
from ordeal import auto_configure
auto_configure()
```

At the end of the test session, the pytest plugin prints a Property Results section:

```
======================== Ordeal Property Results =========================
  PASS  score is never NaN (always: 847 hits)
  PASS  cache hit happens at least once (sometimes: 12 hits)
  PASS  timeout handler runs (reachable: 23 hits)
  FAIL  error recovery path is reachable (reachable: never reached)

  3/4 properties passed
```

One line per property. Type, hit count, and verdict. The `FAIL` line tells you that despite running hundreds of scenarios with faults active, your error recovery path never actually executed. That's a real finding -- either the fault isn't triggering the right condition, or there's a code path issue.


## Naming matters

Each assertion is identified by its name string. This is how the tracker distinguishes properties, how results appear in reports, and how you find problems.

Bad names:

```python
always(x > 0, "check")         # check what?
sometimes(hit, "test")         # test of what?
reachable("here")              # where is "here"?
```

Good names:

```python
always(x > 0, "score is always positive")
sometimes(hit, "cache hit happens at least once")
reachable("error recovery path is reachable")
unreachable("data silently corrupted")
```

A name should read like a sentence. When you see it in a report, you should immediately understand what property passed or failed without looking at the code.


## Examples in context

!!! quote "How to explore this"
    This example shows all four assertion types working together in a real test. Notice how each one plays a different role: `always` guards the math, `sometimes` checks the happy path exists, `reachable` verifies error handling runs, and `unreachable` catches silent corruption. Try writing a `ChaosTest` for your own code and adding one assertion of each type -- you'll be surprised what you discover.

Here's a complete `ChaosTest` using all four assertion types:

```python
import math
from ordeal import ChaosTest, rule, invariant, always, sometimes, reachable, unreachable
from ordeal.faults import timing, numerical, io


class PaymentServiceChaos(ChaosTest):
    faults = [
        timing.timeout("payments.gateway.charge"),
        io.error_on_call("payments.db.save_transaction"),
        numerical.nan_injection("payments.fees.calculate"),
    ]
    swarm = True  # each run uses a random subset of faults

    def __init__(self):
        super().__init__()
        self.service = PaymentService()
        self.processed = []

    @rule()
    def process_payment(self):
        try:
            result = self.service.charge(amount=49.99, user="u-123")
        except TimeoutError:
            # Timeout is expected under fault injection.
            # But we want to verify this path actually runs:
            reachable("timeout handling path")
            return
        except IOError:
            reachable("database error handling path")
            return

        # If charge succeeded, the result must be valid:
        always(not math.isnan(result.fee), "fee is never NaN")
        always(result.amount > 0, "charged amount is positive")

        self.processed.append(result)

    @rule()
    def check_balance(self):
        balance = self.service.get_balance("u-123")

        # Balance must always be a real number, never corrupted:
        always(math.isfinite(balance), "balance is always finite")

        # Across all runs, we should see the balance change at least once:
        sometimes(balance != 0, "balance eventually changes")

    @invariant()
    def no_silent_corruption(self):
        for txn in self.processed:
            if txn.amount != txn.expected_amount:
                unreachable("transaction amount silently changed")

    @invariant()
    def ledger_consistent(self):
        assert self.service.ledger_balanced(), "ledger must balance"


TestPaymentServiceChaos = PaymentServiceChaos.TestCase
```

What each assertion catches:

- **`always(not math.isnan(result.fee), "fee is never NaN")`** -- If `nan_injection` on the fee calculator causes NaN to propagate to the final result, this fires immediately. Hypothesis shrinks to: "activate nan_injection, call process_payment." Now you know your fee calculation doesn't guard against NaN inputs.

- **`sometimes(balance != 0, "balance eventually changes")`** -- If the timeout and IO faults are so aggressive that no payment ever succeeds across the entire session, this fails at the end. It tells you: your test is exercising the error paths but never testing the happy path. Either your faults are too aggressive, or your service can't process anything under load.

- **`reachable("timeout handling path")`** -- If your timeout fault never actually triggers a `TimeoutError` (maybe it targets the wrong function, or the function catches the exception internally), this fails at the end. You thought you were testing timeout handling, but you weren't.

- **`unreachable("transaction amount silently changed")`** -- If a fault causes data corruption that slips past validation -- the amount changes without an error being raised -- this catches it immediately. The worst kind of bug: silent data corruption. This assertion says "if we reach this line, something terrible has happened."


## The Antithesis connection

!!! quote "Why this matters"
    You don't need to know about Antithesis or formal verification to use ordeal. But if you're curious *where* these ideas come from: they're rooted in decades of research on what makes systems correct. The four assertion types aren't arbitrary -- they map to the two fundamental questions of correctness that computer scientists have been studying since the 1970s. Ordeal brings those powerful ideas to you as simple Python functions.

This model of property assertions comes from [Antithesis](https://antithesis.com/docs/using_antithesis/properties.html), a company that builds deterministic simulation testing for distributed systems.

In the Antithesis model, you don't write traditional test assertions that check a single point in time. You declare *properties* -- statements about what should always be true, what should sometimes be true, what should be reachable, what should be unreachable -- and the system accumulates evidence across long-running deterministic simulations.

The insight is that these four types cover the two fundamental categories of correctness:

- **Safety** ("nothing bad happens") -- expressed by `always` and `unreachable`
- **Liveness** ("something good eventually happens") -- expressed by `sometimes` and `reachable`

These terms come from formal verification and temporal logic, but the intuition is simple. Safety is about preventing bad outcomes. Liveness is about ensuring good outcomes. Together, they describe what a correct system looks like.

Ordeal brings this model to Python's testing ecosystem. Instead of simulating for hours on dedicated infrastructure, you get the same property-accumulation semantics running inside pytest, powered by Hypothesis's exploration and shrinking.


!!! quote "You're ready"
    You now have four assertion types in your toolkit: `always` for safety guarantees, `sometimes` for liveness checks, `reachable` for dead code detection, and `unreachable` for silent-failure guards. You know when to use each one. See them in action in the [Writing Tests guide](../guides/writing-tests.md), or explore the full API in the [reference](../reference/api.md).

## Next

- [Chaos Testing](chaos-testing.md) -- how rules, invariants, and the nemesis work together
- [Fault Injection](fault-injection.md) -- the faults that trigger these assertions
