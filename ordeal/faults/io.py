"""I/O fault injections — 10 faults.

Targeted (patch a specific function):
- error_on_call(target) — raise IOError on every call
- return_empty(target) — return None on every call
- corrupt_output(target) — replace output with random bytes
- truncate_output(target, fraction) — cut output to fraction of length
- subprocess_timeout(target) — make matching subprocess calls time out
- subprocess_exit(target) — make matching subprocesses exit nonzero
- subprocess_signal(target) — make matching subprocesses die by signal
- subprocess_truncate_stdout(target, fraction) — truncate subprocess stdout
- subprocess_truncate_stderr(target, fraction) — truncate subprocess stderr

Environment (system-wide, use with caution):
- disk_full() — fail all write-mode open() and os.write()
- permission_denied() — fail all write-mode open()

::

    from ordeal.faults.io import error_on_call, disk_full
    faults = [error_on_call("myapp.db.read"), disk_full()]
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "erroroncall.py",
    "subprocessoutputtruncationfactory.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "faultsio"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
