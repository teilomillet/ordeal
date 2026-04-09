"""ordeal — explores the state space of Python code.

Discovers properties, tests mutations, injects faults, tracks coverage.
Each tool explores one dimension; together they build confidence that
code behaves correctly under all reachable conditions.

``catalog()`` returns every capability at runtime.
``explore(module)`` runs all exploration strategies on a module.
``ordeal.demo`` is a sandbox — any tool works on it.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version
from pathlib import Path


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
    "ordeal.scaling",
    "ordeal.state",
    "ordeal.explore",
    "ordeal.trace",
    "ordeal.supervisor",
    "ordeal.mutagen",
    "ordeal.cmplog",
    "ordeal.concolic",
    "ordeal.grammar",
    "ordeal.equivalence",
)

_SENTINEL = object()



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
        obj = getattr(mod, name, _SENTINEL)
        if obj is not _SENTINEL:
            globals()[name] = obj  # cache for subsequent access
            return obj
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
    return sorted(names)


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
        return [f"ordeal {name} --help", "ordeal catalog --json"] if name else ["ordeal --help"]
    if section == "skill":
        return ["ordeal skill", "ordeal catalog --detail"]
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


def _annotate_catalog_entry(
    section: str,
    entry: dict[str, object],
    *,
    subsystem_summary: str,
) -> dict[str, object]:
    """Attach neutral capability metadata to one catalog entry."""
    annotated = dict(entry)
    qualname = str(annotated.get("qualname", "")).strip()
    module_name, _, attr_name = qualname.rpartition(".")
    obj = _catalog_resolve_object(qualname) if section not in {"cli", "skill"} else None
    signature_meta = _catalog_object_signature(obj)
    parameters = list(signature_meta.get("parameters", []))
    returns = str(signature_meta.get("returns", "")).strip()
    kind = str(signature_meta.get("kind", "unknown")).strip()
    doc = str(annotated.get("doc", "")).strip()
    detail_paragraph = _catalog_detail_paragraph(
        getattr(obj, "__doc__", "") if obj is not None else ""
    )
    module_summary = _catalog_module_summary(module_name)

    capability = str(
        annotated.get("capability")
        or _catalog_first_line(str(annotated.get("description", "")))
        or doc
        or module_summary
        or subsystem_summary
    ).strip()
    applies_to = str(
        annotated.get("applies_to")
        or detail_paragraph
        or (module_summary if module_summary and module_summary != capability else "")
        or _catalog_applies_to_from_parameters(parameters)
        or f"{section} capability surface"
    ).strip()
    inputs = [
        str(item).strip()
        for item in (
            annotated.get("inputs")
            or _catalog_parameter_summaries(parameters)
            or ([str(annotated.get("usage", "")).strip()] if section == "cli" else [])
        )
        if str(item).strip()
    ]
    if not inputs:
        if section == "skill" and str(annotated.get("install", "")).strip():
            inputs = [str(annotated.get("install", "")).strip()]
        elif section == "cli":
            inputs = ["no arguments"]
        elif kind in {"callable", "class"}:
            inputs = ["no parameters"]
    outputs = [
        str(item).strip()
        for item in (
            annotated.get("outputs")
            or _catalog_outputs_from_signature(
                entry_name=str(annotated.get("name", "")),
                kind=kind,
                returns=returns,
            )
        )
        if str(item).strip()
    ]
    learn_more = [
        str(item).strip()
        for item in (annotated.get("learn_more") or _catalog_learn_more(section, annotated))
        if str(item).strip()
    ]
    generated_call_pattern = ""
    if section not in {"cli", "skill"} and module_name and attr_name:
        generated_call_pattern = _catalog_call_pattern(module_name, attr_name, obj) or ""
    call_pattern = str(
        annotated.get("call_pattern") or generated_call_pattern
    ).strip()
    examples = [
        str(item).rstrip()
        for item in (
            annotated.get("examples")
            or ([str(annotated.get("usage", "")).strip()] if section == "cli" else [])
            or ([call_pattern] if call_pattern else [])
        )
        if str(item).strip()
    ]

    if capability:
        annotated["capability"] = capability
    if subsystem_summary:
        annotated["subsystem_summary"] = subsystem_summary
    annotated["applies_to"] = applies_to
    annotated["inputs"] = inputs
    annotated["outputs"] = outputs
    annotated["learn_more"] = learn_more
    if call_pattern:
        annotated["call_pattern"] = call_pattern
    if examples:
        annotated["examples"] = examples
    if parameters:
        annotated["parameters"] = parameters
    if returns:
        annotated["returns"] = returns
    if kind:
        annotated["object_kind"] = kind
    annotated["subsystem"] = section
    annotated["entrypoint"] = section == "cli" or _catalog_entrypoint_name(attr_name, obj)
    return annotated


def _annotate_catalog_section(
    section: str,
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Attach discoverability metadata to one catalog section."""
    subsystem_summary = _catalog_section_summary(section, entries)
    return [
        _annotate_catalog_entry(section, entry, subsystem_summary=subsystem_summary)
        for entry in entries
    ]


def catalog() -> dict[str, list]:
    """Discover all ordeal capabilities via runtime introspection.

    Returns a dict with one key per subsystem — each value is a list of
    dicts describing the available items.  Keys: ``cli``, ``chaos``, ``faults``,
    ``invariants``, ``assertions``, ``strategies``, ``mutations``,
    ``integrations``, ``mining``, ``audit``, ``auto``, ``metamorphic``,
    ``diff``, ``scaling``, ``exploration``, ``trace``, ``supervisor``,
    ``mutagen``, ``cmplog``, ``concolic``, ``grammar``, ``equivalence``.

    Everything is derived from live runtime structures — source introspection
    for Python APIs and the argparse command registry for the CLI. Adding a
    new fault, invariant, or command makes it appear here automatically.

    Each entry now includes neutral discovery metadata for models and tools:
    ``capability`` (what it does), ``applies_to`` (where it is relevant),
    ``inputs`` and ``outputs`` (expected shapes), ``examples`` (usage
    patterns), and ``learn_more`` (adjacent surfaces).

    Example::

        from ordeal import catalog
        c = catalog()
        for key in sorted(c):
            print(f"\\n{key}:")
            for item in c[key]:
                print(f"  {item['qualname']}")
                print(f"    capability: {item['capability']}")
                print(f"    applies_to: {item['applies_to']}")
                print(f"    outputs: {item['outputs']}")
    """
    from ordeal.assertions import catalog as _assertions_catalog
    from ordeal.cli import command_catalog as _cli_catalog
    from ordeal.faults import catalog as _faults_catalog
    from ordeal.invariants import catalog as _invariants_catalog
    from ordeal.mutations import catalog as _mutations_catalog
    from ordeal.strategies import catalog as _strategies_catalog

    result = {
        "cli": _cli_catalog(),
        "faults": _faults_catalog(),
        "invariants": _invariants_catalog(),
        "assertions": _assertions_catalog(),
        "strategies": _strategies_catalog(),
        "mutations": _mutations_catalog(),
        "integrations": _introspect_module(
            __import__("ordeal.integrations.openapi", fromlist=["openapi"]),
        ),
        "mining": _introspect_module(
            __import__("ordeal.mine", fromlist=["mine"]),
        ),
        "audit": _introspect_module(
            __import__("ordeal.audit", fromlist=["audit"]),
        ),
        "auto": _introspect_module(
            __import__("ordeal.auto", fromlist=["auto"]),
        ),
        "metamorphic": _introspect_module(
            __import__("ordeal.metamorphic", fromlist=["metamorphic"]),
        ),
        "diff": _introspect_module(
            __import__("ordeal.diff", fromlist=["diff"]),
        ),
        "scaling": _introspect_module(
            __import__("ordeal.scaling", fromlist=["scaling"]),
        ),
        "chaos": _introspect_module(
            __import__("ordeal.chaos", fromlist=["chaos"]),
            include={"ChaosTest", "RuleTimeoutError", "chaos_test"},
        ),
        "exploration": _introspect_module(
            __import__("ordeal.state", fromlist=["state"]),
        )
        + _introspect_module(
            __import__("ordeal.explore", fromlist=["explore"]),
            include={
                "Explorer",
                "ExplorationResult",
                "CoverageCollector",
                "Checkpoint",
            },
        ),
        "supervisor": _introspect_module(
            __import__("ordeal.supervisor", fromlist=["supervisor"]),
        ),
        "mutagen": _introspect_module(
            __import__("ordeal.mutagen", fromlist=["mutagen"]),
        ),
        "cmplog": _introspect_module(
            __import__("ordeal.cmplog", fromlist=["cmplog"]),
        ),
        "concolic": _introspect_module(
            __import__("ordeal.concolic", fromlist=["concolic"]),
        ),
        "grammar": _introspect_module(
            __import__("ordeal.grammar", fromlist=["grammar"]),
        ),
        "equivalence": _introspect_module(
            __import__("ordeal.equivalence", fromlist=["equivalence"]),
        ),
        "trace": _introspect_module(
            __import__("ordeal.trace", fromlist=["trace"]),
            include={
                "Trace",
                "replay",
                "shrink",
                "ablate_faults",
                "generate_tests",
            },
        ),
    }
    # AI agent skill — discoverable via catalog()
    skill_path = Path(__file__).parent / "SKILL.md"
    if skill_path.exists():
        installed = Path(".claude/skills/ordeal/SKILL.md").exists()
        result["skill"] = [
            {
                "name": "SKILL.md",
                "qualname": "ordeal.SKILL.md",
                "doc": "AI agent skill — local capability map for using ordeal",
                "installed": installed,
                "install": "ordeal skill" if not installed else None,
                "bundled_path": str(skill_path),
            }
        ]

    try:
        result["integrations"].extend(
            _introspect_module(
                __import__("ordeal.integrations.atheris_engine", fromlist=["atheris_engine"]),
            )
        )
    except ImportError:
        pass
    # HTTP endpoint fuzzing (optional: httpx)
    try:
        result["integrations"].extend(
            _introspect_module(
                __import__("ordeal.integrations.http", fromlist=["http"]),
            )
        )
    except ImportError:
        pass
    return {
        section: _annotate_catalog_section(
            section,
            [dict(item) for item in entries],
        )
        for section, entries in result.items()
    }


def _introspect_module(mod: object, include: set[str] | None = None) -> list[dict]:
    """Introspect public callables from a module.

    Auto-filters re-imports by checking ``__module__`` — only functions
    defined in *mod* are returned.  The *include* allowlist is still
    honoured when given, but should no longer be needed for most modules.
    """
    import inspect as _inspect

    mod_name = getattr(mod, "__name__", "")
    entries: list[dict] = []
    for attr_name in sorted(dir(mod)):
        if attr_name.startswith("_"):
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj):
            continue
        # Skip re-imports: only keep functions defined in this module
        obj_mod = getattr(obj, "__module__", None)
        if obj_mod and obj_mod != mod_name:
            continue
        if include and attr_name not in include:
            continue
        try:
            sig = str(_inspect.signature(obj))
        except (ValueError, TypeError):
            sig = "(...)"
        entries.append(
            {
                "name": attr_name,
                "qualname": f"{mod.__name__}.{attr_name}",
                "signature": sig,
                "doc": (_inspect.getdoc(obj) or "").split("\n")[0],
                "call_pattern": _catalog_call_pattern(mod.__name__, attr_name, obj),
            }
        )
    return entries


def auto_configure(
    buggify_probability: float = 0.1,
    seed: int | None = None,
) -> None:
    """Enable chaos testing mode programmatically.

    Alternative to the ``--chaos`` CLI flag.  Call in ``conftest.py``::

        from ordeal import auto_configure
        auto_configure()

    Args:
        buggify_probability: Default probability for ``buggify()`` calls
            (0.0–1.0, default 0.1).
        seed: Random seed for reproducible fault scheduling.
    """
    from ordeal import assertions as _assertions
    from ordeal import buggify as _buggify

    _assertions.tracker.active = True
    _assertions.tracker.reset()
    _buggify.activate(probability=buggify_probability)
    if seed is not None:
        _buggify.set_seed(seed)
