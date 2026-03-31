# AGENTS.md

Conventions for AI agents working on ordeal.

## Build & test

```bash
uv sync                          # install deps
uv run pytest tests/ -q          # run all 205 tests (~7s)
uv run pytest tests/ --chaos     # with property reporting
uv run ordeal explore -c demo.toml  # run explorer (needs PYTHONPATH if tests/ is target)
```

## Project structure

- `ordeal/` — library source (14 modules + faults/ + integrations/)
- `tests/` — 13 test files, includes `test_battle.py` (ordeal testing itself)
- `docs/` — 9 markdown docs, `index.md` is the hub
- `ordeal.toml.example` — annotated config reference

## Rules

- Python 3.12+. Use `match/case`, `type | None` syntax, `from __future__ import annotations`.
- Every public function has a docstring. Every parameter is typed. No untyped `Any` except at system boundaries (wrapping unknown functions, JSON codecs, optional deps).
- Tests go in `tests/test_<module>.py`. Battle tests (ordeal testing itself) go in `tests/test_battle.py`.
- Faults are in `ordeal/faults/{io,numerical,timing}.py`. New fault types go in the matching file or a new one.
- No emojis in code or docs.
- Keep docs under 130 lines each. Example-first, minimal prose.

## Design principle: simple core, depth through parameters

Every function must be simple to call by default. Complexity is unlocked through optional parameters on the SAME function — never by adding a second function.

**Do this:**
```python
sometimes(condition, "name")                              # simple, deferred
sometimes(lambda: fn(), "name", attempts=100)             # depth: immediate retry
```

**Not this:**
```python
sometimes(condition, "name")           # one function for simple case
check_sometimes(fn, "name", attempts=100)  # separate function for advanced case
```

The rule: if you're about to add a new function that does "the same thing but more", add a parameter to the existing function instead. One name, one import, discoverable depth.

This applies everywhere: faults, assertions, invariants, strategies. The user should never need to learn a second API to do more with the same concept.

## Architecture decisions

- `ChaosTest` extends Hypothesis's `RuleBasedStateMachine`. The nemesis rule is auto-injected.
- The Explorer is separate from Hypothesis — it drives rules manually with coverage feedback.
- `buggify()` is a no-op when inactive (thread-local `_state.active` check, negligible overhead).
- Assertions use a global `PropertyTracker` — thread-safe, activated by `--chaos` flag or `auto_configure()`.
- TOML config (`ordeal.toml`) is the interface between humans/agents and the Explorer.
- Traces are JSON. Replay uses recorded param values, not re-drawing from strategies.

## When adding a new feature

1. Write the module in `ordeal/`.
2. Add tests in `tests/test_<module>.py`.
3. Add a battle test in `tests/test_battle.py` if the feature has interesting state.
4. Export from `ordeal/__init__.py` if it's public API.
5. Add a doc in `docs/` (under 130 lines).
6. Update `docs/api-reference.md`.
