# Contributing to ordeal

## Setup

```bash
git clone https://github.com/teilomillet/ordeal
cd ordeal
uv sync --extra dev
```

## Development workflow

```bash
uv run pytest                    # run tests
uv run pytest tests/test_X.py    # single module
uv run pytest -x                 # stop on first failure
uv run ruff check .              # lint
uv run ruff format .             # format
```

## Submitting changes

1. Fork the repo and create a branch from `main`.
2. Write tests for your changes.
3. Ensure `uv run pytest` and `uv run ruff check .` pass.
4. Open a pull request.

## Project structure

See [CLAUDE.md](CLAUDE.md) for architecture, conventions, and common contribution recipes.

## Adding a new fault type

1. Create a function in `ordeal/faults/` that returns a `PatchFault` or `LambdaFault`.
2. Add tests in `tests/test_faults.py`.
3. Document in `docs/api-reference.md`.

## Adding a new assertion

1. Add the function in `ordeal/assertions.py`.
2. Export from `ordeal/__init__.py` and add to `__all__`.
3. Add tests in `tests/test_assertions.py`.
