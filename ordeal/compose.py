"""Long-lived Docker Compose service exploration and probabilistic replay.

The runner keeps one service topology alive while it executes repeated HTTP
operations, injects process and response-boundary faults, and retains captured
JSON values between operations.  Its trace is exact; the external scheduler,
network, and service timing are not.  Replay therefore reports attempts and
exact failure-signature matches instead of claiming deterministic reproduction.
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "sensitivekey.py",
    "httptransport.py",
    "composerunner.py",
    "mutatedpropertytrace.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "compose"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
