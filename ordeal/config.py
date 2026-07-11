"""TOML-driven configuration for ordeal.

Reads ``ordeal.toml`` and returns a typed ``OrdealConfig``.  The config
is the single source of truth for exploration runs — shareable, versionable,
usable by both humans and AI agents.

Minimal example::

    # ordeal.toml
    [explorer]
    target_modules = ["myapp"]
    max_time = 60

    [[tests]]
    class = "tests.test_chaos:MyServiceChaos"

Load it::

    from ordeal.config import load_config
    cfg = load_config()             # reads ./ordeal.toml
    cfg = load_config("ci.toml")    # or a custom path
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "explorerconfig.py",
    "loadcomposeconfig.py",
    "loadconfig.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "config"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
