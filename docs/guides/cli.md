# CLI

## Install

```bash
uv tool install ordeal     # global, `ordeal` on PATH
uvx ordeal explore         # ephemeral, no install
uv run ordeal explore      # inside project venv
```

## Commands

### `ordeal explore`

Run coverage-guided exploration from `ordeal.toml`:

```bash
ordeal explore                          # reads ordeal.toml
ordeal explore -c ci.toml              # custom config
ordeal explore -v                       # live progress
ordeal explore --max-time 300          # override time
ordeal explore --seed 99               # override seed
ordeal explore --no-shrink             # skip failure minimization
```

### `ordeal replay`

Reproduce or minimize a saved trace:

```bash
ordeal replay .ordeal/traces/fail-run-42.json          # reproduce
ordeal replay --shrink trace.json                       # minimize
ordeal replay --shrink trace.json -o minimal.json      # save minimized
```

## pytest integration

ordeal also works as a pytest plugin (auto-registered):

```bash
pytest --chaos                    # enable chaos mode
pytest --chaos --chaos-seed 42    # reproducible seed
pytest --chaos --buggify-prob 0.2 # higher fault probability
```

Mark tests as chaos-only (skipped without `--chaos`):

```python
import pytest

@pytest.mark.chaos
def test_under_chaos():
    ...
```
