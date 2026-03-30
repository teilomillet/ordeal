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
