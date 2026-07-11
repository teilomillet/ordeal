"""CLI entry point for ordeal commands.

$ ordeal explore                    # reads ordeal.toml
$ ordeal explore -c ci.toml         # custom config
$ ordeal explore --max-time 300     # override time
$ ordeal replay .ordeal/traces/run-42.json
$ ordeal replay --shrink trace.json
$ ordeal mine mymod.func            # discover properties
$ ordeal mine mymod.func -n 1000    # more examples
"""

from __future__ import annotations

from pathlib import Path as _FacadePath

from ordeal._facade_loader import load_parts as _load_parts

_PART_FILES = (
    "stderr.py",
    "auditbootstraptargets.py",
    "surfacesupportsummary.py",
    "canonicalsurfacegroupsfortargets.py",
    "objectconfigsuggestionsfromrows.py",
    "auditconfigsuggestions.py",
    "arg.py",
    "cmdcheck.py",
    "renderauditfunctiongapstub.py",
    "resolvescanruntimedefaults.py",
    "runconfiguredscans.py",
    "scanreliability.py",
    "cmdexplorecompose.py",
    "cmdaudit.py",
    "cmdmine.py",
    "cmddiff.py",
    "runinitscan.py",
    "cmdinit.py",
    "cmdmutate.py",
    "formatswarmtelemetry.py",
    "scandetailwithevidence.py",
    "scanbootstraptargetsfromrows.py",
    "renderfindingsection.py",
    "writescanreviewbundleartifacts.py",
    "renderscanregressionfile.py",
    "buildmineagentenvelope.py",
    "buildauditagentenvelope.py",
    "buildreplayagentenvelope.py",
    "replaycomposemanifestrecord.py",
    "recordpostfixcontrol.py",
    "checkspec.py",
    "diffspec.py",
    "commanddescription.py",
    "buildparser.py",
)


def _load_facade_parts() -> None:
    root = _FacadePath(__file__).resolve().parent
    while root.name != "ordeal":
        root = root.parent
    root = root / "parts" / "cli"
    _load_parts(globals(), root, _PART_FILES)


_load_facade_parts()
del _load_facade_parts
