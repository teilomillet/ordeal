# Philosophy

!!! quote "In plain English"
    Your tests probably check what you *expect* to happen. But real bugs come from situations you never imagined — weird combinations of failures hitting at the worst possible time. Ordeal exists to find those bugs before your users do.

## The problem nobody talks about

Your tests pass. All green. You deploy. Then at 2 AM, a request takes 30 seconds instead of 30 milliseconds, a downstream service returns garbage, and your system silently corrupts data for six hours before anyone notices.

This happens because traditional tests verify what you *thought of*. They check the happy path, maybe a few error cases you anticipated. But real failures don't come from scenarios you imagined — they come from combinations you never considered.

A timeout *during* a write. A NaN *inside* a retry loop. A permission error *after* the cache warmed up but *before* the fallback kicked in. The bug isn't in any single component — it's in the space between them.

**That space is enormous. And almost nobody tests it.**

## What chaos testing actually is

!!! quote "Think of it this way"
    Normal testing is like studying for an exam when you already have the questions. Chaos testing is like studying when you *don't* know the questions — so you have to understand the material deeply enough to handle anything. You describe what your code does and what should always be true, and the machine tries thousands of ways to break those promises.

Chaos testing is a simple idea: instead of writing tests for failures you can imagine, let the computer explore failures you can't.

Think of it like this. Traditional testing is a flashlight — you point it at specific dark corners. Chaos testing is a floodlight — it illuminates the entire room, including corners you didn't know existed.

Here's how it works in practice:

1. **You describe your system** — what operations it supports, what faults could happen
2. **The machine explores** — it runs thousands of operation sequences, toggling faults on and off, tracking which code paths it discovers
3. **You state what must be true** — "this should never return NaN," "this path should always be reachable," "data should never be silently lost"
4. **When something breaks**, the machine shrinks the failure to the smallest possible reproducing sequence — so you see exactly what went wrong

You don't write test cases. You describe the world and the rules. The machine finds the violations.

## Why ordeal exists

!!! quote "Why this matters"
    The companies below spent years and millions of dollars building internal tools for this kind of testing. Ordeal takes their best ideas and puts them in a single Python library you can `pip install`. You get the same testing philosophy that protects billion-dollar databases and distributed systems — for free, in your project, today.

The ideas behind ordeal aren't new. They come from some of the most rigorous engineering cultures in the world:

!!! quote "What these names mean for you"
    You don't need to know who Antithesis or FoundationDB are. Here is the short version: each one solved a piece of the "how do I know my code really works?" puzzle. Ordeal combines all the pieces so you don't have to. The Explorer module (`ordeal/explore.py`) handles the coverage-guided search; faults live in `ordeal/faults/`; property assertions live in `ordeal/assertions.py`.

**Antithesis** proved that deterministic exploration finds bugs that random testing misses. By saving checkpoints at interesting states and branching from them, you can systematically cover the space of possible failures. Their insight: coverage-guided exploration beats random fuzzing.

**FoundationDB** showed that you can embed fault injection *inside* the code itself. Their `BUGGIFY` macro is a gate — a no-op in production, a fault trigger during testing. This means the code under test *is* the test harness. No mocks. No fakes. Real code, with probabilistic faults.

**Hypothesis** demonstrated that property-based stateful testing works in practice. Define rules, define invariants, and let a smart engine explore interleavings. When it finds a failure, it shrinks it to the minimal example.

**Jepsen** showed the world that distributed systems need a nemesis — an adversary that injects partitions, kills nodes, corrupts clocks. Without one, you're only testing the happy path of your distributed protocol.

**Jane Street** proved that boundary-biased generation catches more bugs per test run. Most implementation bugs cluster at boundaries: zero, negative one, empty list, maximum length, off-by-one. Bias toward them.

**Meta** showed that mutation testing validates test quality. If you mutate the code (flip a `+` to `-`, change `<` to `<=`) and your tests still pass, your tests have a blind spot.

Ordeal brings all of this to Python. Not as six different tools you have to stitch together — as one library with one API and one mental model.

## The ordeal standard

!!! quote "What this unlocks"
    Imagine being able to say "my code was tested against thousands of failure scenarios I never had to think up." That is what the ordeal standard gives you. It is not a badge you slap on — it is a level of confidence backed by automated exploration, fault injection, and mutation testing all working together.

Here's what we believe:

**If your code passes ordeal, it means something.**

It means an automated explorer ran thousands of operation sequences against your system. It means faults were injected at every level — I/O, timing, numerical — in combinations you never would have written by hand. It means property assertions held across all those runs. It means the code was mutated and your tests caught the mutations.

That's not "the tests pass." That's a *certification* that the code handles adversity.

!!! quote "What this means in practice"
    For a solo developer, this means shipping with evidence your code handles failure — not just a green check, but a trace showing thousands of scenarios explored.

    For a team, this means a shared standard — every PR gets thousands of scenarios tested automatically. No more "did anyone think to test the timeout case?" The machine tests all of them.

    For an organization, this means catching the interaction failures that page your oncall at 3am — the timeout during a retry during a batch write — before they reach production.

We want ordeal to be the bar. When someone looks at a project and sees ordeal traces, they should know: this code was tested the way FoundationDB tests its storage engine. The way Antithesis tests distributed databases. Not at that scale, but with that philosophy.

**Ordeal-tested means the code works.** Not just on the happy path. Not just with the inputs someone thought to try. It works when things go wrong, in combinations nobody anticipated, under conditions that only a machine would think to create.

## Built for what's coming

!!! quote "The key insight"
    Ordeal is designed so that both humans and AI agents can use it the same way. Everything is in config files, deterministic seeds, and machine-readable traces. If an AI generates code, ordeal can tell you whether that code actually handles failure — no human reviewer needed for the mechanics.

Every AI agent generating Python code needs a way to verify that code actually works. Not just "does it run" — does it handle failure? Does it degrade gracefully? Does it maintain its invariants when three things go wrong at once?

Ordeal is designed for this future:

- **TOML configuration** — machines read it as easily as humans
- **Deterministic seeds** — every failure is reproducible, every run is re-runnable
- **Trace files** — JSON records of exactly what happened, step by step
- **Zero implicit knowledge** — everything is explicit, declared, discoverable
- **One command** — `ordeal explore` does the whole thing

An AI agent should be able to: read a codebase, generate an ordeal.toml, run `ordeal explore`, and report whether the code meets the standard. No human in the loop for the mechanics — humans set the invariants, machines verify them.

This isn't theoretical. It's the design constraint that shaped every API decision in ordeal.

## The testing pyramid is incomplete

!!! quote "Think of it this way"
    Unit tests ask "does this function work?" Integration tests ask "do these pieces connect?" End-to-end tests ask "does the whole flow run?" But nobody asks "what happens when two things fail at the same time during step three?" That is the missing layer, and ordeal fills it. It sits underneath everything else because the guarantees it provides support all the tests above.

You know the testing pyramid: unit tests at the base, integration tests in the middle, end-to-end tests at the top. It's a good model. But it's missing a layer.

```
         ╱ ╲
        ╱ E2E ╲
       ╱───────╲
      ╱ Integr.  ╲
     ╱─────────────╲
    ╱   Unit tests   ╲
   ╱─────────────────────╲
  ╱   Chaos + properties    ╲       ← this layer
 ╱───────────────────────────╲
```

Chaos testing with property assertions is the foundation that validates everything above it. Unit tests check individual functions. Integration tests check that components connect. E2E tests check user workflows. But none of them systematically check: **what happens when things go wrong in combination?**

That's the layer ordeal adds.

!!! quote "What you can do with this"
    Whether you are a solo dev shipping a side project or a team maintaining a critical service, the same `ordeal explore` command works. Write an `ordeal.toml`, define your faults and invariants, and let the machine do what machines are good at: exhaustive, tireless exploration of failure combinations.

## The two audiences

Ordeal is designed for two kinds of users, and both matter equally:

**The engineer who wants confidence.** You're shipping a service, a library, an ML pipeline. You want to know it handles failure. You don't want to spend weeks writing failure scenarios by hand. You want to declare your faults, state your invariants, and let the machine do the exploration.

**The AI agent that needs verification.** You're generating code. You need a way to prove it works. Not a test that checks one specific input — a systematic exploration that covers the failure space. You need deterministic, reproducible, machine-readable results.

Both audiences get the same tool. That's the point. The same ordeal.toml that a human writes, an AI agent can generate. The same traces a human reads, an AI agent can parse. The same certification applies to both.

## Principles

!!! quote "In plain English"
    These principles are not abstract rules. They are practical promises. Every API in ordeal was designed around them: you can always reproduce a failure, you can always compose simple pieces into powerful tests, and you never pay a runtime cost in production. If something in ordeal feels surprising, one of these principles probably explains why it works that way.

These are the beliefs that shape ordeal's design:

1. **Explicit over implicit.** Every fault is declared. Every assertion is named. Every configuration key is documented. No hidden behavior, no magic, no "it just works somehow."

2. **Composition over complexity.** Faults compose. Invariants compose with `&`. Rules compose via Hypothesis. You build complex scenarios from simple, understandable pieces.

3. **Reproducibility is non-negotiable.** Every run has a seed. Every failure has a trace. Every trace can be replayed. If you can't reproduce it, you can't fix it.

4. **Negligible overhead in production.** `buggify()` is a no-op when inactive. Assertions are dormant. Faults aren't registered. Ordeal adds nothing meaningful to your production runtime.

5. **Thread-safe by design.** Every shared structure — the PropertyTracker, fault activation, coverage collection, call counters — is lock-guarded. Ordeal is safe for free-threaded Python 3.13+ (no-GIL) out of the box. You don't need to think about it.

6. **The machine explores, the human decides.** You define what "correct" means. The machine finds violations. This separation is fundamental — it's why ordeal works for both humans and AI agents.

7. **Depth over breadth.** One thorough chaos test that explores 10,000 interleavings is worth more than 100 hand-written test cases that each check one scenario. Ordeal favors depth of exploration over volume of test code.

## What's next

Ready to try it?

- **[Getting Started](getting-started.md)** — Write your first chaos test in 5 minutes
- **[Concepts](concepts/chaos-testing.md)** — Understand how ordeal thinks
- **[Explorer Guide](guides/explorer.md)** — Run coverage-guided exploration
