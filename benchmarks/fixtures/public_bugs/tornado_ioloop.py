"""Minimal reproduction of BugsInPy Tornado bug 14.

Upstream fix: https://github.com/tornadoweb/tornado/commit/1d02ed606f1c52636462633d009bdcbaac644331
"""

from __future__ import annotations

from typing import Literal


def force_current_sequence(make_current: Literal[True]) -> dict[str, bool | str]:
    """Force a first current loop, then reject a second loop.

    The upstream bug inverted the forced-current guard, so the valid first
    construction raises before the second-construction assertion is reached.
    """
    state = {"has_current": False}

    def construct_forced() -> None:
        if make_current and not state["has_current"]:
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
