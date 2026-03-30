---
name: ordeal project overview
description: Python chaos testing library — fault injection, property assertions, coverage-guided exploration, stateful testing. Built on Hypothesis, inspired by Antithesis/FoundationDB/Jepsen.
type: project
---

ordeal is an automated chaos testing library for Python combining: ChaosTest (Hypothesis RuleBasedStateMachine + nemesis), coverage-guided Explorer (AFL-style edge hashing), Antithesis-style assertions (always/sometimes/reachable/unreachable), FoundationDB BUGGIFY inline faults, boundary-biased @quickcheck, AST mutation testing, and simulation primitives (Clock, FileSystem).

**Why:** The goal is to make it possible for an LLM to generate a working chaos test from reading source code alone — the "LLM constraint" forces clarity and explicit APIs.

**How to apply:** When working in this codebase, follow the patterns in CLAUDE.md. Use `uv sync --extra dev` for development. Tests pass with `uv run pytest`. Lint with `uv run ruff check .`.
