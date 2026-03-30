"""Pytest plugin for ordeal.

Registers automatically via the ``pytest11`` entry point.

CLI flags:
    --chaos                 Enable chaos testing mode (activates assertions + buggify)
    --chaos-seed SEED       Seed for reproducible chaos
    --buggify-prob FLOAT    Probability for buggify() calls (default 0.1)

Markers:
    @pytest.mark.chaos      Mark a test for chaos mode (collected only with --chaos)
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


def pytest_configure(config: pytest.Config) -> None:
    """Activate assertions + buggify when ``--chaos`` is passed."""
    config.addinivalue_line("markers", "chaos: mark test for chaos mode")
    config.addinivalue_line("markers", "ordeal_scan: auto-generated scan test")

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
    """Skip @pytest.mark.chaos tests unless --chaos is passed."""
    if config.getoption("chaos", default=False):
        return
    skip_chaos = pytest.mark.skip(reason="chaos tests require --chaos flag")
    for item in items:
        if "chaos" in item.keywords:
            item.add_marker(skip_chaos)


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
        from ordeal.auto import fuzz, _resolve_module, _get_public_functions

        mod = _resolve_module(self.module_name)
        func = getattr(mod, self.func_name)
        result = fuzz(func, max_examples=self.max_examples, **(self._fixtures or {}))
        if not result.passed:
            raise OrdealScanError(result.summary())

    def repr_failure(self, excinfo: pytest.ExceptionInfo[BaseException], style: str | None = None) -> str:
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
        **kwargs: Any,
    ) -> None:
        super().__init__(name, parent, **kwargs)
        self.module_name = module_name
        self.max_examples = max_examples
        self._fixtures = fixtures

    def collect(self) -> list[pytest.Item]:
        from ordeal.auto import _get_public_functions, _infer_strategies, _resolve_module

        try:
            mod = _resolve_module(self.module_name)
        except ImportError as e:
            self.warn(pytest.PytestWarning(f"Cannot import {self.module_name}: {e}"))
            return []

        items = []
        for func_name, func in _get_public_functions(mod):
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

    from ordeal.config import load_config, ConfigError

    try:
        cfg = load_config(str(file_path))
    except (ConfigError, FileNotFoundError):
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
    """Print the Ordeal Property Results section at the end of the test run."""
    # Only show property report when --chaos is active
    if not config.getoption("chaos", default=False):
        return

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
