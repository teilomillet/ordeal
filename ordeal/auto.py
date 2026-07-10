"""Zero-boilerplate testing. Point at code, get tests.

Three primitives:

1. **scan_module** — smoke-test every public function::

       result = scan_module("myapp.scoring")
       assert result.passed

2. **fuzz** — deep-fuzz a single function::

       result = fuzz(myapp.scoring.compute)
       result = fuzz(myapp.scoring.compute, model=model_strategy)

3. **chaos_for** — auto-generate a ChaosTest from a module's API::

       TestScoring = chaos_for("myapp.scoring", invariants=[finite])
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

_PART_FILES = (
    "functionresult.py",
    "seedexample.py",
    "appendseedexample.py",
    "projectevidenceindex.py",
    "seedexamplesforcallable.py",
    "scancrashpromoted.py",
    "expandcontractnames.py",
    "applymodelinferencepack.py",
    "lifecyclefaultruntime.py",
    "singleresolvedhint.py",
    "makeboundmethodcallable.py",
    "shellinjectionprobekwargs.py",
    "analyzeshellstatements.py",
    "lifecyclefollowupcontract.py",
    "inferstrategies.py",
    "mineobjectharnesshintscached.py",
    "testseedexamplescached.py",
    "semanticbucket.py",
    "reachabilityscore.py",
    "buildproofbundle.py",
    "semanticcontractgate.py",
    "scanmodule.py",
    "testonefunction.py",
    "fuzz.py",
    "chaosfor.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "auto"
    namespace = globals()
    for filename in _PART_FILES:
        path = root / filename
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), namespace, namespace)


_load_facade_parts()
del _load_facade_parts
