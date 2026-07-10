from __future__ import annotations
# ruff: noqa
import re
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version
from pathlib import Path
from types import ModuleType
def _source_tree_version() -> str | None:
    """Return the version declared by the local source tree when available."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    match = re.search(
        r'(?m)^version\s*=\s*"([^"]+)"\s*$',
        pyproject.read_text(encoding="utf-8"),
    )
    return match.group(1) if match is not None else None
def _resolve_version() -> str:
    """Prefer the installed package version, then fall back to the source tree."""
    try:
        return _get_version("ordeal")
    except PackageNotFoundError:
        return _source_tree_version() or "0.0.0+unknown"
__version__ = _resolve_version()
__all__ = [
    # Core
    "ChaosTest",
    "RuleTimeoutError",
    "chaos_test",
    # Assertions
    "always",
    "declare",
    "sometimes",
    "reachable",
    "unreachable",
    "report",
    "ReliabilityCell",
    # Buggify
    "buggify",
    "buggify_value",
    # Hypothesis re-exports
    "rule",
    "invariant",
    "initialize",
    "precondition",
    "Bundle",
    # Config
    "auto_configure",
    # Discoverability
    "catalog",
    # Mutations
    "mutate",
    "mutate_function_and_test",
    "MutationResult",
    "PRESETS",
    "OPERATORS",
    "NoTestsFoundError",
    "generate_starter_tests",
    "init_project",
    # Migration workflow
    "migrate",
    "MigrationResult",
    # Evidence
    "verify_bug_evidence",
    "BugEvidenceVerification",
    # Everything in _LAZY_SUBMODULES is also importable via
    # ``from ordeal import X`` — see __getattr__ and __dir__.
]
# Submodules whose public exports are re-exported from ordeal.
# Add a public function or class to any of these → it becomes
# importable via ``from ordeal import X`` with zero registration.
_STATEFUL_EXPORTS = ("Bundle", "initialize", "invariant", "precondition", "rule")
_LAZY_SUBMODULES = (
    "ordeal.assertions",
    "ordeal.buggify",
    "ordeal.chaos",
    "ordeal.mutations",
    "ordeal.mine",
    "ordeal.audit",
    "ordeal.auto",
    "ordeal.metamorphic",
    "ordeal.diff",
    "ordeal.system_diff",
    "ordeal.migration",
    "ordeal.scaling",
    "ordeal.evidence",
    "ordeal.state",
    "ordeal.explore",
    "ordeal.compose",
    "ordeal.trace",
    "ordeal.supervisor",
    "ordeal.mutagen",
    "ordeal.cmplog",
    "ordeal.concolic",
    "ordeal.grammar",
    "ordeal.equivalence",
)
_SENTINEL = object()
_CALLABLE_SUBMODULES = frozenset({"ordeal.audit", "ordeal.diff", "ordeal.mine"})
class _CallableEntrypointModule(ModuleType):
    """A submodule that also delegates calls to its same-named entrypoint."""

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Call the function whose name matches this submodule's final component."""
        name = self.__name__.rpartition(".")[2]
        entrypoint = getattr(self, name, None)
        if not callable(entrypoint):
            raise TypeError(f"module {self.__name__!r} has no callable {name!r}")
        return entrypoint(*args, **kwargs)
def _make_callable_entrypoint_module(module: ModuleType) -> None:
    """Preserve module imports while making true entrypoint collisions callable."""
    if module.__name__ in _CALLABLE_SUBMODULES and not isinstance(
        module, _CallableEntrypointModule
    ):
        module.__class__ = _CallableEntrypointModule
class _OrdealPackage(ModuleType):
    """Prepare explicitly imported colliding submodules when accessed."""

    def __getattribute__(self, name: str) -> object:
        """Keep child modules intact while making their public entrypoints callable."""
        value = super().__getattribute__(name)
        if isinstance(value, ModuleType):
            _make_callable_entrypoint_module(value)
        return value
sys.modules[__name__].__class__ = _OrdealPackage
def __getattr__(name: str) -> object:
    """Lazy import: search submodules for the requested name."""
    import importlib

    if name in _STATEFUL_EXPORTS:
        from hypothesis import stateful as _stateful

        obj = getattr(_stateful, name)
        globals()[name] = obj
        return obj

    for mod_path in _LAZY_SUBMODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        _make_callable_entrypoint_module(mod)
        obj = getattr(mod, name, _SENTINEL)
        if obj is not _SENTINEL:
            leaf_name = mod_path.rpartition(".")[2]
            resolved = mod if name == leaf_name and callable(mod) else obj
            globals()[name] = resolved  # cache for subsequent access
            return resolved
    raise AttributeError(f"module 'ordeal' has no attribute {name!r}")
def __dir__() -> list[str]:
    """Include lazy submodule exports in dir() for tab completion."""
    import importlib
    import inspect as _inspect

    names = set(globals().keys())
    names.update(_STATEFUL_EXPORTS)
    names.update(__all__)
    for mod_path in _LAZY_SUBMODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        _make_callable_entrypoint_module(mod)
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            obj = getattr(mod, attr, None)
            if obj is None:
                continue
            # Only list things defined in that module (skip re-imports)
            obj_mod = getattr(obj, "__module__", None)
            if obj_mod == mod_path or (_inspect.isclass(obj) and obj_mod == mod_path):
                names.add(attr)
    _restore_lazy_entrypoint_collisions()
    return sorted(names)
def _restore_lazy_entrypoint_collisions() -> None:
    """Keep colliding child modules intact and callable after discovery imports."""
    for module_name in _CALLABLE_SUBMODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        _make_callable_entrypoint_module(module)
        globals()[module_name.rpartition(".")[2]] = module
def _catalog_call_pattern(
    module_name: str,
    attr_name: str,
    obj: object | None = None,
) -> str | None:
    """Return a neutral import-plus-call pattern for one catalog entry."""
    if not module_name or not attr_name:
        return None
    import_line = f"from {module_name} import {attr_name}"
    if obj is None:
        return f"{import_line}\n{attr_name}(...)"

    import inspect as _inspect

    try:
        sig = _inspect.signature(obj)
    except (TypeError, ValueError):
        return f"{import_line}\n{attr_name}(...)"

    args: list[str] = []
    for index, param in enumerate(sig.parameters.values()):
        if index >= 3:
            args.append("...")
            break
        if param.kind is _inspect.Parameter.VAR_POSITIONAL:
            args.append(f"*{param.name}")
            continue
        if param.kind is _inspect.Parameter.VAR_KEYWORD:
            args.append(f"**{param.name}")
            continue
        if param.default is _inspect.Signature.empty:
            args.append(param.name)
        else:
            args.append(f"{param.name}={param.name}")
    joined = ", ".join(args)
    return f"{import_line}\n{attr_name}({joined})"
def _catalog_first_line(text: str) -> str:
    """Return the first non-empty line from *text*."""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
def _catalog_detail_paragraph(text: str) -> str:
    """Return the first descriptive paragraph after the summary line."""
    paragraphs = [" ".join(block.split()) for block in str(text or "").split("\n\n")]
    filtered = [
        paragraph
        for paragraph in paragraphs
        if paragraph
        and not paragraph.lower().startswith(("args:", "returns:", "example", "examples:"))
    ]
    return filtered[1] if len(filtered) > 1 else ""
def _catalog_module_summary(module_name: str) -> str:
    """Return the first line of one module docstring when importable."""
    if not module_name:
        return ""
    import importlib

    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return ""
    return _catalog_first_line(getattr(mod, "__doc__", "") or "")
def _catalog_resolve_object(qualname: str) -> object | None:
    """Resolve one qualified runtime object when possible."""
    module_name, _, attr_name = str(qualname or "").rpartition(".")
    if not (module_name and attr_name):
        return None
    import importlib

    try:
        obj: object = importlib.import_module(module_name)
    except Exception:
        return None
    try:
        for part in attr_name.split("."):
            obj = getattr(obj, part)
    except Exception:
        return None
    return obj
def _catalog_annotation_text(annotation: object) -> str:
    """Render one annotation into compact text."""
    import inspect as _inspect

    if annotation is _inspect.Signature.empty:
        return ""
    if isinstance(annotation, str):
        return annotation
    if getattr(annotation, "__module__", "") == "builtins" and getattr(annotation, "__name__", ""):
        return str(annotation.__name__)
    text = repr(annotation)
    return text.removeprefix("typing.")
def _catalog_default_text(value: object) -> str:
    """Render one default value into compact text."""
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:37]}..."
def _catalog_object_signature(obj: object | None) -> dict[str, object]:
    """Return structured signature metadata for one runtime object."""
    import inspect as _inspect

    if obj is None:
        return {"kind": "unknown", "parameters": [], "returns": ""}
    kind = "class" if _inspect.isclass(obj) else "callable"
    try:
        sig = _inspect.signature(obj)
    except (TypeError, ValueError):
        return {"kind": kind, "parameters": [], "returns": ""}

    parameters: list[dict[str, object]] = []
    for param in sig.parameters.values():
        annotation = _catalog_annotation_text(param.annotation)
        has_default = param.default is not _inspect.Signature.empty
        parameters.append(
            {
                "name": param.name,
                "kind": param.kind.name.lower(),
                "annotation": annotation,
                "required": not has_default,
                "default": _catalog_default_text(param.default) if has_default else None,
            }
        )
    return {
        "kind": kind,
        "parameters": parameters,
        "returns": _catalog_annotation_text(sig.return_annotation),
    }
def _catalog_parameter_summaries(parameters: list[dict[str, object]]) -> list[str]:
    """Render structured signature metadata into compact input summaries."""
    summaries: list[str] = []
    for item in parameters[:6]:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        text = name
        annotation = str(item.get("annotation", "")).strip()
        if annotation:
            text += f": {annotation}"
        default = item.get("default")
        if default is not None and str(default).strip():
            text += f" = {default}"
        summaries.append(text)
    return summaries
def _catalog_outputs_from_signature(
    *,
    entry_name: str,
    kind: str,
    returns: str,
) -> list[str]:
    """Infer output summaries from signature metadata."""
    if returns:
        return [returns]
    if kind == "class":
        return [f"{entry_name} instance"]
    return ["Python return value"]
def _catalog_applies_to_from_parameters(parameters: list[dict[str, object]]) -> str:
    """Infer a neutral applicability hint from parameter names."""
    names = {str(item.get("name", "")).strip().lower() for item in parameters}
    hints: list[str] = []
    if {"target", "targets"} & names:
        hints.append("named callable or module targets")
    if {"module", "modules"} & names:
        hints.append("module-level inputs")
    if "trace_file" in names:
        hints.append("saved trace files")
    if "finding_id" in names:
        hints.append("saved finding identifiers")
    if "config" in names:
        hints.append("config-driven runs")
    return ", ".join(dict.fromkeys(hints))
def _catalog_entrypoint_name(name: str, obj: object | None) -> bool:
    """Return whether one object is re-exported from the top-level package."""
    top_level = globals().get(name, _SENTINEL)
    if top_level is not _SENTINEL:
        return True
    return obj is not None and name in __all__
def _catalog_learn_more(section: str, entry: dict[str, object]) -> list[str]:
    """Return generic adjacent discovery surfaces for one entry."""
    if section == "cli":
        name = str(entry.get("name", "")).strip()
        if name == "diff":
            return [
                "ordeal diff --help",
                "docs/guides/revision-diff.md",
                "docs/guides/revision-diff-troubleshooting.md",
                "docs/reference/revision-diff-schema.md",
                "docs/concepts/differential-testing.md",
                "docs/concepts/divergence-evidence.md",
                "docs/guides/divergence-evidence.md",
                "docs/guides/divergence-evidence-troubleshooting.md",
                "docs/reference/divergence-evidence-schema.md",
                "ordeal catalog --json",
            ]
        return [f"ordeal {name} --help", "ordeal catalog --json"] if name else ["ordeal --help"]
    if section == "skill":
        return ["ordeal skill", "ordeal catalog --detail"]
    if section == "diff":
        return [
            "docs/concepts/differential-testing.md",
            "docs/concepts/system-differential.md",
            "docs/guides/differential-quickstart.md",
            "docs/guides/differential-state-and-effects.md",
            "docs/guides/differential-evidence.md",
            "docs/concepts/divergence-evidence.md",
            "docs/guides/divergence-evidence.md",
            "docs/guides/divergence-evidence-troubleshooting.md",
            "docs/reference/divergence-evidence-schema.md",
            "docs/guides/revision-diff.md",
            "docs/guides/revision-diff-troubleshooting.md",
            "docs/reference/revision-diff-schema.md",
            "docs/guides/system-differential.md",
            "docs/guides/system-differential-recipes.md",
            "docs/guides/system-differential-troubleshooting.md",
            "docs/reference/system-differential.md",
        ]
    if section == "migration":
        return [
            "docs/concepts/safe-migrations.md",
            "docs/guides/migration-workflow.md",
            "docs/reference/api.md#migration-workflow",
            "ordeal migrate --help",
        ]
    qualname = str(entry.get("qualname", "")).strip()
    module_name, _, _ = qualname.rpartition(".")
    hints = ["from ordeal import catalog; catalog()", "ordeal catalog --detail"]
    if module_name:
        hints.insert(0, module_name)
    return list(dict.fromkeys(hints))
def _catalog_section_summary(section: str, entries: list[dict[str, object]]) -> str:
    """Return a neutral summary for one catalog section."""
    summaries: list[str] = []
    for entry in entries:
        qualname = str(entry.get("qualname", "")).strip()
        module_name, _, _ = qualname.rpartition(".")
        summary = _catalog_module_summary(module_name)
        if summary and summary not in summaries:
            summaries.append(summary)
    if summaries:
        return summaries[0]
    if section == "cli":
        return "CLI commands derived from argparse."
    if section == "skill":
        return "Bundled local skill guidance."
    return f"{section} capabilities discovered at runtime."
