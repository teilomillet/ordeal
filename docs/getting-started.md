# Getting Started

From zero to your first chaos test in 5 minutes.

## Install

```bash
pip install ordeal
```

Or with uv:

```bash
uv add ordeal               # add to project
uv tool install ordeal       # install CLI globally
```

## Your first chaos test

A chaos test defines three things:
1. **Faults** — what can go wrong (timeout, NaN, crash)
2. **Rules** — what the system does (process input, read data)
3. **Invariants** — what must always hold (no corruption, no data loss)

```python
# test_chaos.py
import math
from ordeal import ChaosTest, rule, invariant, always
from ordeal.faults import timing, numerical

class ScoreServiceChaos(ChaosTest):
    faults = [
        timing.timeout("myapp.api.fetch_data"),
        numerical.nan_injection("myapp.model.predict"),
    ]

    def __init__(self):
        super().__init__()
        self.service = ScoreService()

    @rule()
    def score_user(self):
        try:
            result = self.service.score("user-123")
        except TimeoutError:
            return  # timeouts are expected when the fault is active
        always(not math.isnan(result), "score is never NaN")
        always(0 <= result <= 1, "score in valid range")

    @invariant()
    def service_is_healthy(self):
        assert self.service.is_healthy()

# This line makes pytest discover and run it
TestScoreServiceChaos = ScoreServiceChaos.TestCase
```

## Run it

```bash
# Standard pytest (Hypothesis drives exploration)
pytest test_chaos.py -v

# With chaos mode (enables assertion tracking + buggify)
pytest test_chaos.py --chaos

# Reproducible with a seed
pytest test_chaos.py --chaos --chaos-seed 42
```

## What happens

1. Hypothesis generates random sequences of rules
2. The **nemesis** (auto-injected) randomly toggles your faults on and off
3. After every step, all `@invariant` methods are checked
4. If anything fails, Hypothesis **shrinks** to the minimal reproducing case

Output:
```
FAILED test_chaos.py::TestScoreServiceChaos::runTest
  Falsifying example:
    state = ScoreServiceChaos()
    state._nemesis(data=...)      # activates nan_injection
    state.score_user()            # NaN propagates to output
    state.teardown()
```

## Next steps

- [Core Concepts](core-concepts.md) — understand faults, nemesis, swarm, assertions
- [Explorer Guide](explorer.md) — coverage-guided deep exploration
- [Configuration](configuration.md) — `ordeal.toml` for reproducible runs
