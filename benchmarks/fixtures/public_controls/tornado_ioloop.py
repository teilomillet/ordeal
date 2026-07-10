"""Fixed sibling of the BugsInPy Tornado bug 14 reproduction.

Upstream fix: https://github.com/tornadoweb/tornado/commit/1d02ed606f1c52636462633d009bdcbaac644331
"""

from __future__ import annotations

from typing import Literal


def force_current_sequence(make_current: Literal[True]) -> dict[str, bool | str]:
    """Force a first current loop, then reject a second loop.

    This models both assertions in the upstream ``test_force_current`` test:
    the first construction succeeds and the second fails without replacing it.
    """
    state = {"has_current": False}

    def construct_forced() -> None:
        if make_current and state["has_current"]:
            raise RuntimeError("current IOLoop already exists")
        state["has_current"] = True

    construct_forced()
    try:
        construct_forced()
    except RuntimeError as exc:
        return {
            "current_preserved": state["has_current"],
            "second_error": str(exc),
        }
    raise AssertionError("second forced loop unexpectedly succeeded")
