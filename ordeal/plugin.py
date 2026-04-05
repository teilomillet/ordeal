"""Pytest plugin for ordeal.

Registers automatically via the ``pytest11`` entry point.  No
configuration needed — pytest discovers it on import.

**What ``--chaos`` activates (3 things):**

1. ``PropertyTracker`` — ``always()``/``sometimes()``/``reachable()``/
   ``unreachable()`` results are recorded for the property report.
   (Note: ``always()`` and ``unreachable()`` raise on violation
   regardless of ``--chaos`` — violations are never silent.)
2. ``buggify()`` — returns ``True`` probabilistically instead of
   always returning ``False``.
3. ``@pytest.mark.chaos`` tests — collected instead of skipped.

**What works WITHOUT ``--chaos``:**

- ``ChaosTest.TestCase`` — Hypothesis drives rule/nemesis exploration.
- ``@invariant()`` with ``assert`` — standard Python assertions.
- ``always()`` / ``unreachable()`` — raise on violation (always).
- Faults — the nemesis toggles them regardless.

CLI flags::

    --chaos                 Enable chaos testing mode
    --chaos-seed SEED       Seed for reproducible chaos
    --buggify-prob FLOAT    Probability for buggify() calls (default 0.1)
    --rule-timeout FLOAT    Per-rule timeout for ChaosTest (seconds, default 30; 0 to disable)

Markers::

    @pytest.mark.chaos      Mark a test for chaos mode (skipped without --chaos)

Fixtures::

    chaos_enabled           Activate chaos for a single test (no --chaos needed)
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from ordeal import assertions
from ordeal.buggify import activate as _buggify_activate
from ordeal.buggify import deactivate as _buggify_deactivate
from ordeal.buggify import set_seed as _buggify_set_seed


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--chaos``, ``--chaos-seed``, and ``--buggify-prob`` CLI flags."""
    group = parser.getgroup("ordeal", "Chaos testing with ordeal")
    group.addoption(
        "--chaos",
        action="store_true",
        default=False,
        help="Enable chaos testing mode.",
    )
    group.addoption(
        "--chaos-seed",
        type=int,
        default=None,
        help="Seed for deterministic chaos reproduction.",
    )
    group.addoption(
        "--buggify-prob",
        type=float,
        default=0.1,
        help="Probability for buggify() calls (default: 0.1).",
    )
    group.addoption(
        "--rule-timeout",
        type=float,
        default=None,
        help="Per-rule timeout for ChaosTest in seconds (default: 30, 0 to disable).",
    )
    group.addoption(
        "--mutate",
        action="store_true",
        default=False,
        help="Run mutation testing on @pytest.mark.mutate tests.",
    )
    group.addoption(
        "--mutate-preset",
        type=str,
        default="standard",
        choices=["essential", "standard", "thorough"],
        help="Mutation operator preset (default: standard).",
    )


def _register_array_strategies() -> None:
    """Register Hypothesis strategies for ML array types if available.

    Detects MLX, JAX, and PyTorch and registers ``st.from_type``
    strategies so ``scan_module`` and ``@quickcheck`` can auto-test
    functions that accept array parameters without SmallSearchSpaceWarning.
    """
    from hypothesis import strategies as st

    def _array_strategy(shape: tuple[int, ...] = (3, 4)) -> st.SearchStrategy:
        """Generate small random arrays for property testing."""
        import numpy as np

        return st.builds(
            lambda: np.random.default_rng(42).standard_normal(shape).astype(np.float32),
        )

    # MLX
    try:
        import mlx.core as mx

        st.register_type_strategy(
            mx.array,
            _array_strategy().map(lambda a: mx.array(a)),
        )
    except (ImportError, Exception):
        pass

    # JAX
    try:
        import jax.numpy as jnp

        st.register_type_strategy(
            jnp.ndarray,
            _array_strategy().map(lambda a: jnp.array(a)),
        )
    except (ImportError, Exception):
        pass

    # PyTorch
    try:
        import torch

        st.register_type_strategy(
            torch.Tensor,
            _array_strategy().map(lambda a: torch.from_numpy(a)),
        )
    except (ImportError, Exception):
        pass

    # NumPy (explicit, avoids from_type fallback warning)
    try:
        import numpy as np

        st.register_type_strategy(
            np.ndarray,
            _array_strategy(),
        )
    except (ImportError, Exception):
        pass


_seed_replay_results: list[dict[str, Any]] = []


def _replay_seed_corpus() -> list[dict[str, Any]]:
    """Scan .ordeal/seeds/ and replay all seeds.  Returns replay results."""
    from pathlib import Path

    from ordeal.trace import Trace
    from ordeal.trace import replay as _replay

    corpus = Path(".ordeal/seeds")
    if not corpus.exists():
        return []

    # Try loading config for custom corpus_dir
    try:
        from ordeal.config import load_config

        cfg = load_config()
        corpus = Path(cfg.report.corpus_dir)
    except Exception:
        pass

    if not corpus.exists():
        return []

    results: list[dict[str, Any]] = []
    for class_dir in sorted(corpus.iterdir()):
        if not class_dir.is_dir():
            continue
        for seed_file in sorted(class_dir.glob("seed-*.json")):
            try:
                trace = Trace.load(seed_file)
            except Exception:
                continue
            error = _replay(trace)
            results.append(
                {
                    "path": str(seed_file),
                    "seed_name": seed_file.stem,
                    "reproduced": error is not None,
                    "error": f"{type(error).__name__}: {error}" if error else None,
                    "test_class": trace.test_class,
                }
            )
    return results


def pytest_configure(config: pytest.Config) -> None:
    """Activate assertions + buggify when ``--chaos`` is passed."""
    config.addinivalue_line("markers", "chaos: mark test for chaos mode")
    config.addinivalue_line("markers", "ordeal_scan: auto-generated scan test")
    config.addinivalue_line(
        "markers",
        'mutate(target, preset="standard"): run mutation testing on target',
    )

    _register_array_strategies()

    # Replay seed corpus for regression detection — runs on EVERY pytest
    # invocation, not just --chaos.  This is the Go fuzzing model: saved
    # crashers are permanent regression tests that run automatically.
    _seed_replay_results.clear()
    try:
        _seed_replay_results.extend(_replay_seed_corpus())
    except Exception:
        pass  # seed replay is best-effort

    # -- rule_timeout: CLI flag → ordeal.toml → class default (30s) --
    rule_timeout = config.getoption("rule_timeout", default=None)
    if rule_timeout is None:
        try:
            from ordeal.config import load_config

            cfg = load_config()
            rule_timeout = cfg.explorer.rule_timeout
        except Exception:
            pass
    if rule_timeout is not None:
        from ordeal.chaos import ChaosTest

        ChaosTest.rule_timeout = float(rule_timeout)

    if config.getoption("chaos", default=False):
        assertions.tracker.active = True
        assertions.tracker.reset()

        prob = config.getoption("buggify_prob", default=0.1)
        _buggify_activate(probability=prob)

        seed = config.getoption("chaos_seed", default=None)
        if seed is not None:
            _buggify_set_seed(seed)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Deactivate assertions and buggify on session teardown."""
    assertions.tracker.active = False
    _buggify_deactivate()


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip @pytest.mark.chaos and @pytest.mark.mutate tests unless flags are passed.

    When chaos tests are skipped, a warning is emitted in the terminal
    summary so CI pipelines don't silently pass with zero chaos coverage.
    """
    if not config.getoption("chaos", default=False):
        skip_chaos = pytest.mark.skip(reason="chaos tests require --chaos flag")
        chaos_count = 0
        for item in items:
            if "chaos" in item.keywords:
                item.add_marker(skip_chaos)
                chaos_count += 1
        if chaos_count:
            # Store the count so pytest_terminal_summary can warn
            config._ordeal_skipped_chaos = chaos_count

    if not config.getoption("mutate", default=False):
        skip_mutate = pytest.mark.skip(reason="mutation tests require --mutate flag")
        for item in items:
            if "mutate" in item.keywords:
                item.add_marker(skip_mutate)


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def chaos_enabled() -> Generator[None, None, None]:
    """Activate chaos mode (assertions + buggify) for a single test."""
    prev_active = assertions.tracker.active
    assertions.tracker.active = True
    _buggify_activate()
    yield
    _buggify_deactivate()
    assertions.tracker.active = prev_active


# Storage for mutation results collected during the session
_mutation_results: list[tuple[str, Any]] = []


@pytest.fixture
def mutate_target(request: pytest.FixtureRequest):
    """Run mutation testing on the target specified in @pytest.mark.mutate.

    Usage::

        @pytest.mark.mutate("myapp.scoring.compute", preset="standard")
        def test_scoring(mutate_target):
            result = mutate_target  # MutationResult
            assert result.score >= 0.8

    The mutation result is also collected for the terminal summary.
    """
    from ordeal.mutations import mutate

    marker = request.node.get_closest_marker("mutate")
    if marker is None:
        pytest.skip("no @pytest.mark.mutate marker")
        return

    target = marker.args[0] if marker.args else None
    if target is None:
        pytest.fail("@pytest.mark.mutate requires a target path")
        return

    preset = marker.kwargs.get("preset") or request.config.getoption(
        "mutate_preset", default="standard"
    )
    workers = marker.kwargs.get("workers", 1)

    result = mutate(target, preset=preset, workers=workers)
    _mutation_results.append((target, result))
    return result


# -- Auto-scan test collection from ordeal.toml ----------------------------


class OrdealScanItem(pytest.Item):
    """A single auto-scanned function test item."""

    def __init__(
        self,
        name: str,
        parent: pytest.Collector,
        module_name: str = "",
        func_name: str = "",
        max_examples: int = 50,
        fixtures: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, parent, **kwargs)
        self.module_name = module_name
        self.func_name = func_name
        self.max_examples = max_examples
        self._fixtures = fixtures
        self.add_marker(pytest.mark.ordeal_scan)

    def runtest(self) -> None:
        from ordeal.auto import _resolve_module, fuzz

        mod = _resolve_module(self.module_name)
        func = getattr(mod, self.func_name)
        result = fuzz(func, max_examples=self.max_examples, **(self._fixtures or {}))
        if not result.passed:
            raise OrdealScanError(result.summary())

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: str | None = None,
    ) -> str:
        if isinstance(excinfo.value, OrdealScanError):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)  # type: ignore[arg-type]

    def reportinfo(self) -> tuple[str, int | None, str]:
        return (self.module_name, None, f"ordeal::scan::{self.module_name}.{self.func_name}")


class OrdealScanError(Exception):
    """Raised when an auto-scanned function fails."""


class OrdealScanCollector(pytest.Collector):
    """Collects scan items for one [[scan]] entry."""

    def __init__(
        self,
        name: str,
        parent: pytest.Collector,
        module_name: str = "",
        max_examples: int = 50,
        fixtures: dict[str, object] | None = None,
        expected_failures: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, parent, **kwargs)
        self.module_name = module_name
        self.max_examples = max_examples
        self._fixtures = fixtures
        self._expected_failures = expected_failures or []

    def collect(self) -> list[pytest.Item]:
        from ordeal.auto import _get_public_functions, _infer_strategies, _resolve_module

        try:
            mod = _resolve_module(self.module_name)
        except ImportError as e:
            self.warn(pytest.PytestWarning(f"Cannot import {self.module_name}: {e}"))
            return []

        items = []
        for func_name, func in _get_public_functions(mod):
            if func_name in self._expected_failures:
                continue
            strategies = _infer_strategies(func, self._fixtures)
            if strategies is None:
                continue
            items.append(
                OrdealScanItem.from_parent(
                    self,
                    name=func_name,
                    module_name=self.module_name,
                    func_name=func_name,
                    max_examples=self.max_examples,
                    fixtures=self._fixtures,
                )
            )
        return items


def pytest_collect_file(parent: pytest.Collector, file_path: Any) -> OrdealScanCollector | None:
    """Auto-collect scan tests from ordeal.toml if present."""
    # Only trigger once, on ordeal.toml itself
    if not str(file_path).endswith("ordeal.toml"):
        return None

    from ordeal.config import ConfigError, load_config

    try:
        cfg = load_config(str(file_path))
    except (ConfigError, FileNotFoundError, UnicodeDecodeError, ValueError):
        return None

    if not cfg.scan:
        return None

    # Create a collector per scan entry
    # We return just the first one; the rest are collected via pytest_collect_modifyitems
    # Actually, we need a parent collector that yields children
    return _OrdealTomlCollector.from_parent(
        parent,
        path=file_path,
        scan_configs=cfg.scan,
    )


class _OrdealTomlCollector(pytest.File):
    """Collects all [[scan]] entries from ordeal.toml."""

    def __init__(
        self,
        *args: Any,
        scan_configs: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._scan_configs = scan_configs or []

    def collect(self) -> list[pytest.Item | pytest.Collector]:
        items: list[pytest.Item | pytest.Collector] = []
        for scan_cfg in self._scan_configs:
            fixtures = _parse_toml_fixtures(scan_cfg.fixtures)
            collector = OrdealScanCollector.from_parent(
                self,
                name=f"scan[{scan_cfg.module}]",
                module_name=scan_cfg.module,
                max_examples=scan_cfg.max_examples,
                fixtures=fixtures,
                expected_failures=scan_cfg.expected_failures,
            )
            items.extend(collector.collect())
        return items


def _parse_toml_fixtures(raw: dict[str, str]) -> dict[str, Any] | None:
    """Convert TOML fixture strings to Hypothesis strategies.

    Supports: ``"a,b,c"`` → ``sampled_from(["a","b","c"])``.
    """
    import hypothesis.strategies as _st

    if not raw:
        return None
    fixtures: dict[str, Any] = {}
    for name, value in raw.items():
        if isinstance(value, str) and "," in value:
            fixtures[name] = _st.sampled_from(value.split(","))
        elif isinstance(value, str):
            fixtures[name] = _st.just(value)
        else:
            fixtures[name] = _st.just(value)
    return fixtures


# -- Terminal report --------------------------------------------------------


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print Ordeal Property Results and Mutation Results at the end."""
    # -- Warn if chaos tests were skipped (easy to miss in CI) --
    skipped = config.__dict__.get("_ordeal_skipped_chaos", 0) if hasattr(config, "__dict__") else 0
    if skipped:
        terminalreporter.section("Ordeal Warning")
        terminalreporter.line(
            f"WARNING: {skipped} chaos test(s) SKIPPED — run with --chaos to include them",
        )
        terminalreporter.line(
            "  Without --chaos, buggify() is inactive and chaos-marked tests don't run.",
        )
        terminalreporter.line("")

    # -- Seed corpus replay results --
    if _seed_replay_results:
        terminalreporter.section("Ordeal Seed Corpus")
        reproduced = sum(1 for s in _seed_replay_results if s["reproduced"])
        fixed = len(_seed_replay_results) - reproduced
        terminalreporter.line(
            f"  {len(_seed_replay_results)} seed(s) replayed: "
            f"{fixed} fixed, {reproduced} reproduced",
            green=(reproduced == 0),
            red=(reproduced > 0),
        )
        for sr in _seed_replay_results:
            if sr["reproduced"]:
                terminalreporter.line(
                    f"  REGRESSION  {sr['seed_name']}: {sr['test_class']} — {sr['error']}",
                    red=True,
                )
        _seed_replay_results.clear()

    # -- Mutation results (from @pytest.mark.mutate tests) --
    if _mutation_results:
        terminalreporter.section("Ordeal Mutation Results")
        for target, result in _mutation_results:
            terminalreporter.line(result.summary())
            terminalreporter.line("")
        _mutation_results.clear()

    # Show property report when --chaos is active or when there are results
    results = assertions.tracker.results
    if not results:
        return

    terminalreporter.section("Ordeal Property Results")

    passed = [p for p in results if p.passed]
    failed = [p for p in results if not p.passed]

    for p in passed:
        terminalreporter.line(f"  PASS  {p.summary}", green=True)
    for p in failed:
        terminalreporter.line(f"  FAIL  {p.summary}", red=True)

    total = len(results)
    if failed:
        terminalreporter.line(f"\n  {len(failed)}/{total} properties FAILED", red=True)
    else:
        terminalreporter.line(f"\n  {total}/{total} properties passed", green=True)

    # Structured metadata — no hardcoded advice, just facts.
    seed = config.getoption("chaos_seed", default=None)
    kinds = {p.type for p in results if isinstance(getattr(p, "type", None), str)}
    terminalreporter.line("")
    terminalreporter.line(
        f"  Config: seed={'none' if seed is None else seed}, "
        f"assertion types used: {', '.join(sorted(kinds)) or 'none'}",
    )
