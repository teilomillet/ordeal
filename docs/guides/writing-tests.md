---
description: >-
  Patterns for writing effective chaos tests in Python with ordeal.
  ChaosTest examples, fault combinations, invariant design, and
  real-world recipes.
---

# Writing Tests

!!! quote "This page is your cookbook"
    Everything on this page is a pattern you can copy, adapt, and combine. You don't need to memorize it all at once. Start with the first pattern, get a test running, then come back for more when you're ready.

    By the end of this page, you'll know how to write chaos tests for any system — from a single function to a full distributed service. Every pattern here is a tool you're adding to your belt.

Practical patterns for writing effective chaos tests with ordeal. This guide assumes you have read [Getting Started](../getting-started.md) and understand the three ingredients: faults, rules, invariants.

---

## Test structure

!!! quote "In plain English"
    A chaos test is just a class with three things: a list of what can break (faults), a set of actions your system performs (rules), and a set of things that must always be true (invariants). Ordeal then runs your actions in hundreds of different orders, randomly flipping faults on and off, and checks that your invariants hold every single time.

    All chaos tests extend the `ChaosTest` base class from `ordeal/chaos.py`. That class wires everything together — you just fill in the pieces.

Every chaos test has the same shape: a class that extends `ChaosTest`, declares faults, defines rules for operations, and adds invariants for health checks. Here is a complete, well-structured test with annotations:

```python
import math
from ordeal import ChaosTest, rule, invariant, initialize, always, sometimes
from ordeal.faults import timing, numerical, io


class PaymentServiceChaos(ChaosTest):
    """Chaos test for the payment processing service.

    Faults model real production failures:
    - The payment gateway times out under load
    - The fraud scoring model occasionally returns NaN
    - The receipt writer fails when disk is full
    """

    # --- Faults: what can go wrong ---
    faults = [
        timing.timeout("payments.gateway.charge"),
        numerical.nan_injection("payments.fraud.score"),
        io.error_on_call("payments.receipts.write", error=IOError),
    ]

    # --- State: initialize the system under test ---
    def __init__(self):
        super().__init__()
        self.service = PaymentService()
        self.processed_count = 0
        self.failed_count = 0

    # --- Rules: operations the system performs ---
    @rule()
    def process_payment(self):
        try:
            result = self.service.charge("user-1", amount=49.99)
        except (TimeoutError, IOError):
            self.failed_count += 1
            return  # expected when faults are active
        self.processed_count += 1
        always(result.amount == 49.99, "charge amount is preserved")
        always(result.receipt_id is not None, "charge produces a receipt")

    @rule()
    def check_balance(self):
        balance = self.service.get_balance("user-1")
        always(
            not math.isnan(balance),
            "balance is never NaN",
        )

    # --- Invariants: checked after every step ---
    @invariant()
    def ledger_is_balanced(self):
        assert self.service.total_debits() == self.service.total_credits()

    @invariant()
    def no_negative_balances(self):
        for account in self.service.all_accounts():
            assert account.balance >= 0

    # --- Cleanup ---
    def teardown(self):
        self.service.reset()
        super().teardown()


# Pytest discovery: ClassName + .TestCase
TestPaymentServiceChaos = PaymentServiceChaos.TestCase
```

**Naming convention.** The class is named `<System>Chaos` -- in this case `PaymentServiceChaos`. The test case for pytest discovery is `Test<System>Chaos = <System>Chaos.TestCase`. This pattern is required: Hypothesis stateful machines expose a `TestCase` attribute, and pytest discovers classes prefixed with `Test`.

---

## Choosing faults

!!! quote "How to decide what faults to use"
    Walk through your code and ask: "what calls could fail in production?" Every HTTP request, database query, file write, and external API call is a candidate. You don't need to guess which ones matter most — ordeal's swarm mode will figure that out. Your job is just to list the realistic failure modes.

    The fault functions live in `ordeal/faults/` — `timing.py` for slow and crashing calls, `io.py` for disk and network errors, `numerical.py` for math gone wrong. Each one takes a dotted path to the function you want to break.

Start with two questions:

**1. What external dependencies can fail?**

Every call that leaves your process is a candidate. Network calls time out. Databases return errors. File systems run out of space. Third-party APIs return garbage.

```python
from ordeal.faults import timing, io

faults = [
    # The payment gateway is an external HTTP call -- it can time out
    timing.timeout("payments.gateway.charge"),

    # We write receipts to disk -- disk can be full
    io.disk_full(),

    # We call a third-party fraud API -- it can fail
    io.error_on_call("payments.fraud.check", error=ConnectionError),
]
```

**2. What internal computations can go wrong?**

Numerical code produces NaN and Inf. ML models return wrong shapes. Serialization corrupts data.

```python
from ordeal.faults import numerical

faults = [
    # The scoring model can return NaN on edge-case inputs
    numerical.nan_injection("scoring.model.predict"),

    # The embedding layer can return wrong dimensions after a bad update
    numerical.wrong_shape("scoring.model.embed", expected=(1, 512), actual=(1, 256)),

    # The normalization step can produce Inf on near-zero inputs
    numerical.inf_injection("scoring.normalize"),
]
```

**The fault library at a glance:**

| Module | Faults | Use when |
|---|---|---|
| `faults.timing` | `timeout`, `slow`, `intermittent_crash`, `jitter` | External calls, latency-sensitive code |
| `faults.io` | `error_on_call`, `return_empty`, `corrupt_output`, `truncate_output`, `disk_full`, `permission_denied` | File I/O, database calls, API calls |
| `faults.numerical` | `nan_injection`, `inf_injection`, `wrong_shape`, `corrupted_floats` | Math, ML models, data pipelines |

Each fault takes a `target` -- a dotted path to the function to patch (e.g., `"myapp.api.call"`). When the nemesis activates the fault, that function gets replaced with the faulty version. When the nemesis deactivates it, the original is restored.

---

## Writing good rules

!!! quote "Think of it this way"
    Rules are the actions your system can perform. Each rule is one thing a user, service, or background job might do — like "process a payment" or "read the cache." Ordeal mixes and matches these actions in every possible order, with faults flickering on and off, to see if your system stays healthy.

    The magic happens because you write simple, individual actions, and ordeal explores the combinations. You don't need to think about ordering — that's ordeal's job.

Rules represent operations that users, services, or background jobs perform on your system. Think of them as the verbs of your test: "process a payment," "read the cache," "update a record."

**Keep rules atomic.** One operation per rule. Don't combine "create user and then charge them" into a single rule -- make two rules. Hypothesis explores rule orderings, so separate rules give it more to work with.

```python
# Good: one operation per rule
@rule()
def create_user(self):
    self.service.create_user("alice")

@rule()
def charge_user(self):
    try:
        self.service.charge("alice", 10.0)
    except TimeoutError:
        return

# Bad: two operations in one rule
@rule()
def create_and_charge(self):
    self.service.create_user("alice")
    self.service.charge("alice", 10.0)  # fault might fire between these
```

**Handle expected exceptions.** When you declare a `timeout` fault, your rule will see `TimeoutError`. That is not a bug -- it is the expected consequence of the fault. Catch it and move on. The *bug* is when the system handles the timeout incorrectly (corrupts state, loses data, returns garbage).

```python
@rule()
def fetch_data(self):
    try:
        data = self.service.fetch("key-1")
    except TimeoutError:
        # Timeout is expected. The system should recover gracefully.
        # The invariant below will catch it if the system is now broken.
        return
    except ConnectionError:
        return

    # Assertions go AFTER the operation, not before
    always(data is not None, "fetch returns data when it succeeds")
    always(len(data) > 0, "fetched data is non-empty")
```

**Place assertions after the operation.** The assertion checks the result of an operation. It goes after the call, not before. If the operation raised an expected exception, you already returned.

**Use Hypothesis strategies for input variation.** Rules can draw from strategies to vary their inputs:

```python
import hypothesis.strategies as st

@rule(amount=st.floats(min_value=0.01, max_value=10000.0))
def charge(self, amount):
    try:
        result = self.service.charge("user-1", amount)
    except TimeoutError:
        return
    always(result.amount == amount, "charged amount matches request")
```

---

## Writing good invariants

!!! quote "The key insight"
    An invariant is a promise about your system that must be true at all times — not just when things go well, but even when faults are active. Think of it like a safety net: "no matter what chaos is happening, the ledger must always balance" or "no account balance can ever go negative."

    If you're not sure what invariants to write, ask yourself: "what would be really bad if it happened?" That's your invariant.

Invariants are checked after every single step -- after every rule execution, after every nemesis toggle. They must be:

1. **Cheap.** They run hundreds or thousands of times per test. Don't call external services, don't do heavy computation.
2. **Always true.** An invariant must hold regardless of which faults are active, regardless of the order of operations. If it can legitimately be false sometimes, it is not an invariant.
3. **Structural, not behavioral.** Invariants check that the system is structurally sound: no corruption, no inconsistency, no impossible states. Don't put business logic in invariants.

```python
# Good invariants: structural health checks

@invariant()
def ledger_balanced(self):
    """Total debits must always equal total credits."""
    assert self.service.total_debits() == self.service.total_credits()

@invariant()
def no_orphan_records(self):
    """Every transaction must reference a valid account."""
    for txn in self.service.all_transactions():
        assert self.service.account_exists(txn.account_id)

@invariant()
def cache_consistent(self):
    """Cached values must match the source of truth."""
    for key in self.cache.keys():
        if self.cache.get(key) is not None:
            assert self.cache.get(key) == self.store.get(key)
```

```python
# Bad invariants

@invariant()
def payment_succeeds(self):
    # This is business logic, not a structural check.
    # Payments will fail when faults are active -- that's expected.
    assert self.service.charge("user", 10.0) is not None

@invariant()
def model_is_accurate(self):
    # This is expensive and not always true under fault injection.
    assert self.service.evaluate_model() > 0.95
```

**Composable invariants.** For repeated numeric checks, use the invariants module:

```python
from ordeal.invariants import no_nan, no_inf, bounded

valid_score = no_nan & no_inf & bounded(0, 1)

@invariant()
def scores_valid(self):
    for score in self.service.all_scores():
        valid_score(score)
```

---

## Using assertions effectively

!!! quote "What you can do with this"
    Assertions go inside your rules to check specific results. Ordeal gives you four kinds, each answering a different question: "must this always be true?" (`always`), "should this happen at least once?" (`sometimes`), "does this error path actually run?" (`reachable`), and "should this never happen?" (`unreachable`).

    Together, they let you express both safety ("nothing bad happens") and liveness ("good things eventually happen"). These assertions live in `ordeal/assertions.py` and are all exported from `ordeal.__init__`.

Ordeal provides four assertion types, inspired by [Antithesis](https://antithesis.com). Each serves a different purpose.

### `always` -- safety properties

"This must be true every time this line executes." Raises immediately on violation, which triggers Hypothesis shrinking.

Use for properties that must never be violated, no matter what faults are active:

```python
@rule()
def withdraw(self):
    try:
        self.account.withdraw(50)
    except InsufficientFunds:
        return
    always(self.account.balance >= 0, "balance never goes negative")
```

If a timeout fault causes the balance check to be skipped and the balance goes negative, `always` catches it and Hypothesis shrinks to the minimal reproduction.

### `sometimes` -- liveness properties

"This must be true at least once across all runs." Checked at session end, not immediately.

Use to verify that the happy path works at least some of the time. Under fault injection, most operations will fail -- but they should succeed at least occasionally:

```python
@rule()
def process_order(self):
    try:
        result = self.service.process(order)
    except (TimeoutError, IOError):
        return
    if result.success:
        sometimes(True, "orders succeed at least sometimes")
```

If faults are so aggressive that no order ever succeeds, `sometimes` flags it. This catches over-aggressive error handling (e.g., a retry loop that gives up too quickly).

### `reachable` -- error paths execute

"This code path must execute at least once." Checked at session end.

Use to verify that your error handling actually runs. Dead error-handling code is a liability -- it looks like protection but has never been tested:

```python
@rule()
def call_api(self):
    try:
        self.client.fetch("/data")
    except TimeoutError:
        reachable("timeout-handler")
        self.client.use_cached_fallback()
    except ConnectionError:
        reachable("connection-error-handler")
        self.client.mark_unhealthy()
```

If neither handler ever runs during chaos testing, you know your faults are not reaching these code paths -- either the fault configuration is wrong, or the code path is genuinely unreachable.

### `unreachable` -- impossible states

"This code path must never execute." Raises immediately on violation.

Use for states that your system design says are impossible:

```python
@rule()
def reconcile(self):
    debits = self.ledger.total_debits()
    credits = self.ledger.total_credits()
    if debits != credits:
        unreachable("ledger-imbalanced")
```

If a fault causes an imbalanced ledger, `unreachable` catches it immediately.

---

## Swarm mode

!!! quote "Why this matters"
    When you have lots of faults, turning them all on at once drowns your system — everything fails and you don't learn much. Swarm mode solves this by picking a random subset of faults for each run. One run might test "timeout + NaN", another might test "disk full + IO error." Across many runs, you get better total coverage than all-faults-always-on ever could.

    Just set `swarm = True` on your test class and ordeal handles the rest. If a bug is found, Hypothesis shrinks to the minimal subset of faults that caused it.

By default, every run has access to all declared faults. The nemesis can toggle any of them. **Swarm mode** changes this: each run randomly selects a *subset* of faults.

```python
class PaymentServiceChaos(ChaosTest):
    faults = [
        timing.timeout("payments.gateway.charge"),
        timing.slow("payments.gateway.authorize"),
        numerical.nan_injection("payments.fraud.score"),
        io.error_on_call("payments.receipts.write", error=IOError),
        io.disk_full(),
    ]
    swarm = True  # each run uses a random subset
```

**When to enable swarm mode:**

- You have 3 or more faults. With many faults, all-on-at-once drowns the exploration in noise. Swarm mode lets some runs focus on timeout + NaN while others focus on disk_full + IO error.
- You want better *aggregate* coverage across many runs. Individual runs explore fewer combinations, but the total coverage across all runs is higher (Groce et al., 2012).

**When to keep swarm off:**

- You have 1-2 faults. There are not enough combinations to benefit from subsetting.
- You want a specific, deterministic fault schedule for debugging.
- You are investigating a known issue and want all faults active to reproduce it.

Hypothesis controls the swarm mask, so shrinking still works -- it finds the minimal subset of faults that triggers the failure.

---

## State management

!!! quote "In plain English"
    State is the memory your test keeps between steps — things like "how many payments have been processed" or "what's currently in the cache." You need just enough state to verify that your system is behaving correctly, but not so much that the test becomes hard to understand. Think of it as a notebook where you jot down the important facts so you can check them later.

**Initialize in `__init__`.** Set up the system under test and any tracking state:

```python
class CacheChaos(ChaosTest):
    faults = [...]

    def __init__(self):
        super().__init__()  # always call super().__init__()
        self.cache = LRUCache(max_size=100)
        self.reference = {}  # shadow state for verification
```

**Use `@initialize()` for Hypothesis-controlled setup.** If setup involves random choices (which database to connect to, which configuration to use), let Hypothesis drive it:

```python
@initialize(config=st.sampled_from(["small", "medium", "large"]))
def setup_cache(self, config):
    sizes = {"small": 10, "medium": 100, "large": 1000}
    self.cache = LRUCache(max_size=sizes[config])
```

This lets Hypothesis explore different configurations and shrink to the one that triggers the failure.

**Clean up in `teardown`.** Deactivate faults, reset shared state, close connections:

```python
def teardown(self):
    self.cache.clear()
    self.reference.clear()
    super().teardown()  # always call super().teardown() -- it resets faults
```

**Keep state minimal.** Every piece of state is a dimension that Hypothesis needs to explore. A test with 10 fields requires exponentially more runs to cover than one with 3. Track only what you need for assertions and invariants.

---

## Common patterns

!!! quote "What this unlocks"
    Each pattern below is a recipe you can copy into your project and adapt. They cover the most common systems people test with ordeal: services that retry, caches that might serve stale data, multi-stage pipelines, and systems with multiple actors. Start with whichever one looks most like your system, then mix and match ideas from the others.

    You don't need to use all of these. Even one pattern, well applied, will catch bugs that hand-written tests miss.

### The retry test

A service retries failed operations. The fault makes the underlying call fail intermittently. The test verifies that retries actually work and that the system eventually succeeds.

```python
from ordeal import ChaosTest, rule, invariant, always, sometimes
from ordeal.faults import timing


class RetryServiceChaos(ChaosTest):
    faults = [
        timing.intermittent_crash("myapp.api.send", every_n=3),
    ]

    def __init__(self):
        super().__init__()
        self.service = RetryService(max_retries=5)
        self.successes = 0

    @rule()
    def send_request(self):
        try:
            result = self.service.send({"action": "ping"})
        except RuntimeError:
            # Even with retries, some calls may exhaust all attempts
            return
        self.successes += 1
        always(result.status == "ok", "successful sends return ok")

    @invariant()
    def retry_count_bounded(self):
        assert self.service.total_retries <= self.service.total_attempts * 5

    def teardown(self):
        sometimes(self.successes > 0, "retries succeed at least sometimes")
        super().teardown()


TestRetryServiceChaos = RetryServiceChaos.TestCase
```

### The cache test

Rules populate and read the cache. Faults corrupt or evict entries. The test verifies that the cache never serves stale or corrupted data.

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import io
from ordeal.faults import LambdaFault


class CacheChaos(ChaosTest):
    faults = [
        io.corrupt_output("myapp.cache.serialize"),
        LambdaFault(
            "evict-all",
            on_activate=lambda: None,  # actual eviction happens in rule
            on_deactivate=lambda: None,
        ),
    ]

    def __init__(self):
        super().__init__()
        self.cache = Cache()
        self.reference = {}  # ground truth

    @rule()
    def put(self):
        key, value = "key-1", "value-1"
        self.cache.put(key, value)
        self.reference[key] = value

    @rule()
    def get(self):
        result = self.cache.get("key-1")
        if result is not None:
            # If the cache returns something, it must be correct
            always(
                result == self.reference.get("key-1"),
                "cache returns correct value",
            )

    @rule()
    def evict(self):
        self.cache.clear()
        # Reference stays -- we track what SHOULD be there vs what IS there

    @invariant()
    def size_bounded(self):
        assert self.cache.size() <= self.cache.max_size


TestCacheChaos = CacheChaos.TestCase
```

### The pipeline test

!!! quote "How to explore this"
    If your system processes data in stages — extract, transform, load, or anything similar — this pattern is for you. Each stage gets its own fault and its own rule. Ordeal explores what happens when stage 1 fails but stage 2 succeeds, or when stage 3 fails after stage 2 already transformed the data. These partial-failure scenarios are where the nastiest bugs hide.

A data pipeline has multiple stages, each with its own dependencies. Faults target each dependency independently.

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical, io


class PipelineChaos(ChaosTest):
    faults = [
        timing.timeout("pipeline.extract.fetch"),       # stage 1: extract
        numerical.nan_injection("pipeline.transform.normalize"),  # stage 2: transform
        io.error_on_call("pipeline.load.write", error=IOError),   # stage 3: load
    ]
    swarm = True  # explore different fault combinations per stage

    def __init__(self):
        super().__init__()
        self.pipeline = DataPipeline()

    @rule()
    def run_extract(self):
        try:
            self.pipeline.extract()
        except TimeoutError:
            return
        always(self.pipeline.raw_data is not None, "extract produces data")

    @rule()
    def run_transform(self):
        if self.pipeline.raw_data is None:
            return  # nothing to transform yet
        try:
            self.pipeline.transform()
        except ValueError:
            return  # NaN caused a validation error -- expected

    @rule()
    def run_load(self):
        if self.pipeline.transformed_data is None:
            return  # nothing to load yet
        try:
            self.pipeline.load()
        except IOError:
            return

    @invariant()
    def no_partial_writes(self):
        """Data is either fully loaded or not loaded at all."""
        if self.pipeline.is_loaded:
            assert self.pipeline.load_count == self.pipeline.transform_count


TestPipelineChaos = PipelineChaos.TestCase
```

### The concurrent-actors test

!!! quote "Think of it this way"
    In real systems, multiple actors do things at the same time — a customer buys an item while the warehouse restocks while an admin checks inventory. This pattern gives each actor its own rule. Ordeal interleaves them in every possible order, with faults adding delays and crashes, mimicking the kind of timing-dependent bugs that are nearly impossible to reproduce by hand.

Rules represent different actors (user, admin, background worker). Faults create scenarios similar to race conditions.

```python
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing


class MultiActorChaos(ChaosTest):
    faults = [
        timing.slow("inventory.db.update", delay=0.5),
        timing.intermittent_crash("inventory.db.read", every_n=5),
    ]

    def __init__(self):
        super().__init__()
        self.inventory = InventoryService()
        self.inventory.restock("widget", 100)

    @rule()
    def customer_buys(self):
        """A customer purchases an item."""
        try:
            self.inventory.purchase("widget", quantity=1)
        except (RuntimeError, TimeoutError):
            return  # out of stock or service error

    @rule()
    def warehouse_restocks(self):
        """The warehouse restocks items."""
        try:
            self.inventory.restock("widget", 10)
        except RuntimeError:
            return

    @rule()
    def admin_audits(self):
        """An admin reads the inventory count."""
        try:
            count = self.inventory.count("widget")
        except RuntimeError:
            return
        always(count >= 0, "inventory never goes negative")

    @invariant()
    def stock_non_negative(self):
        assert self.inventory.count("widget") >= 0


TestMultiActorChaos = MultiActorChaos.TestCase
```

---

## Common mistakes

!!! quote "Why this matters"
    Everyone makes these mistakes when starting out — they're not obvious until someone points them out. Skim through this section now so you recognize the patterns, and come back here when a test isn't behaving the way you expect. Each mistake has a simple fix.

### Asserting inside fault declarations

Faults are declarations. They describe *what can go wrong*, not *what to check*. Assertions go in rules and invariants.

```python
# Wrong: assertion in the fault list
faults = [
    timing.timeout("api.call"),
    # Don't do this -- there is no place for assertions here
]

# Right: assertions in rules
@rule()
def call_api(self):
    try:
        result = self.service.call()
    except TimeoutError:
        return
    always(result.is_valid(), "API returns valid data")
```

### Forgetting to handle expected exceptions

When you declare `timing.timeout("api.call")`, your rule WILL see `TimeoutError` when that fault is active. If you don't catch it, the test fails with an unhandled exception -- but that is not the bug you are looking for.

```python
# Wrong: unhandled expected exception
@rule()
def call_api(self):
    result = self.service.call()  # raises TimeoutError when fault is active
    always(result is not None, "result exists")

# Right: catch expected exceptions
@rule()
def call_api(self):
    try:
        result = self.service.call()
    except TimeoutError:
        return  # expected -- the system should handle this gracefully
    always(result is not None, "result exists")
```

The rule of thumb: if the fault you declared causes exception X, catch X in your rules.

### Making invariants expensive

Invariants run after every single step. If you have 50 steps per run and 200 runs, that is 10,000 invariant checks. Keep them fast.

```python
# Wrong: expensive invariant
@invariant()
def full_consistency_check(self):
    # This queries every record, recomputes every hash, and verifies every
    # foreign key. It takes 200ms per call.
    assert self.service.deep_consistency_check()

# Right: cheap structural check
@invariant()
def count_consistent(self):
    assert self.service.record_count() == len(self.service.index)
```

If you need an expensive check, do it in a rule (which runs occasionally) or in `teardown` (which runs once).

### Too many faults without swarm mode

With 8 faults and no swarm mode, the nemesis can toggle any of them at any time. The system is overwhelmed -- every operation fails, and the test never exercises interesting partial-failure scenarios.

```python
# Problematic: 8 faults, all active at once
class OverloadedChaos(ChaosTest):
    faults = [f1, f2, f3, f4, f5, f6, f7, f8]
    # swarm defaults to False -- every run can toggle all 8

# Better: enable swarm mode
class OverloadedChaos(ChaosTest):
    faults = [f1, f2, f3, f4, f5, f6, f7, f8]
    swarm = True  # each run uses a random subset
```

---

## Scaling up

!!! quote "What to expect as your project grows"
    One test class per component is all you need. As your project grows, you add more test files — not more complexity to existing ones. Each test file targets one component with its own faults. The Explorer (configured via `ordeal.toml` and run with `ordeal explore`) ties them all together with coverage-guided exploration across your entire system.

For a single service, one `ChaosTest` class is enough. For a larger codebase, organize chaos tests by component.

### One test class per component

```
tests/
    test_chaos_payments.py      # PaymentServiceChaos
    test_chaos_inventory.py     # InventoryServiceChaos
    test_chaos_notifications.py # NotificationServiceChaos
```

Each file tests one component in isolation, with faults targeting that component's dependencies.

### Use ordeal.toml for configuration

When you have multiple chaos tests, configure them centrally:

```toml
# ordeal.toml
[explorer]
target_modules = ["myapp"]
max_time = 120
seed = 42

[[tests]]
class = "tests.test_chaos_payments:PaymentServiceChaos"
swarm = true

[[tests]]
class = "tests.test_chaos_inventory:InventoryServiceChaos"
steps_per_run = 100

[report]
format = "both"
traces = true
```

Then run with the Explorer for thorough coverage-guided exploration:

```bash
ordeal explore -v
```

### CI integration

In CI, run chaos tests with a fixed seed for reproducibility:

```bash
# Fast: Hypothesis-driven, good for every PR
pytest tests/test_chaos_*.py --chaos --chaos-seed 42

# Thorough: Explorer-driven, good for nightly runs
ordeal explore --config ordeal.toml
```

Start with a short `max_time` (30-60 seconds) and increase it as your test suite matures. The Explorer's coverage guidance means even short runs find interesting states that random testing misses.
