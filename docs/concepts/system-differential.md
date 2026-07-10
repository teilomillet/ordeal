---
title: System Differential Testing
description: A plain-language explanation of comparing whole workflows safely.
---

# System differential testing

Imagine replacing the engine in a delivery van. Starting the new engine once is
not enough. You drive the usual route, climb a hill, stop in traffic, lose GPS,
restart, and check that every parcel still arrives. A software refactor deserves
the same test.

Function comparison asks: “Given the same input, do these two functions return
the same answer?” System comparison asks a larger question: “After the same
story happens, are these two versions still observably the same?”

## The story Ordeal replays

A story is one ordered sequence containing two kinds of event:

- an **operation** is something a user or another service does, such as add an
  item, save an order, or request a balance;
- a **fault event** changes the environment, such as activating a timeout,
  corrupting a response, crashing a worker, or clearing the fault.

Ordeal constructs a fresh old system and a fresh new system. It gives both the
same operation arguments and the same fault plan, in the same order. Neither
version gets to see what happened inside the other.

```text
put order → enable timeout → read order → clear timeout → read order
     │              identical story sent to both versions              │
     ├── old version: timeout, then recovers                            │
     └── new version: timeout, then stays broken  ← divergence ─────────┘
```

## What “the same” includes

The answer is wider than matching return values:

1. **Interface:** are the same public names present with the same signatures?
2. **Outcome:** did both return comparable values, or raise the same exception
   type and message?
3. **State:** after every event, does meaningful business state match?
4. **Side effects:** did both emit the same selected events, writes, or calls?
5. **Recovery:** after a fault is cleared or a worker restarts, do later clean
   operations behave the same?

If any measured contract differs, the result is `divergent`.

## Why the sequence becomes smaller

A 40-step failure is hard to understand. Ordeal removes events while requiring
the exact first mismatch to remain. The result might become:

```text
enable timeout → clear timeout → read order
```

That is not merely shorter output. It is an explanation: the new version does
not recover after the timeout is cleared. Ordeal reruns the minimized story and
reports `attempted N / reproduced M`, because real services can still contain
timing outside Python's control.

## Why speed is reported separately

Correct behavior and acceptable speed answer different questions. A refactor
can preserve every output and still become five times slower. It can also be
faster while returning the wrong answer.

For that reason, a failed `PerformanceBudget` never changes semantic
`result.status`. Review behavior parity and the performance result as two
measured contracts.

## What a passing run proves

`no_divergence_observed` means the measured interfaces and the events in this
sequence matched. It does not prove that every possible sequence matches.
Coverage grows by adding representative operations, fault plans, and state or
side-effect probes.

Next, follow the [copyable first run](../guides/system-differential.md), then use
the [recipes](../guides/system-differential-recipes.md) for APIs and performance.
