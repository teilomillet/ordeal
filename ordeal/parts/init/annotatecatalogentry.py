from __future__ import annotations
# ruff: noqa
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
    call_pattern = str(annotated.get("call_pattern") or generated_call_pattern).strip()
    examples = [
        str(item).rstrip()
        for item in (
            annotated.get("examples")
            or ([str(annotated.get("usage", "")).strip()] if section == "cli" else [])
            or ([call_pattern] if call_pattern else [])
        )
        if str(item).strip()
    ]
    if section == "diff" and attr_name == "diff":
        examples = [
            (
                "from ordeal import diff\n"
                "result = diff(old_fn, new_fn)\n"
                "print(result.status, result.witness)"
            ),
            (
                "from ordeal import Operation, diff\n"
                "result = diff(OldSystem, NewSystem, "
                "sequence=[Operation('read')])"
            ),
        ]
    if section == "migration" and attr_name == "migrate":
        examples = [
            (
                "from ordeal import ContractCheck, migrate\n"
                "rule = ContractCheck(\n"
                "    'score stays between 0 and 1',\n"
                "    predicate=lambda value: 0 <= value <= 1,\n"
                "    kwargs={'features': [0.2, 0.4]},\n"
                ")\n"
                "result = migrate(\n"
                "    'oldpkg.scoring', 'newpkg.scoring',\n"
                "    invariants={'score': [rule]},\n"
                ")"
            )
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
    ``diff``, ``migration``, ``scaling``, ``evidence``, ``exploration``, ``trace``,
    ``supervisor``,
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
        )
        + _introspect_module(
            __import__("ordeal.system_diff", fromlist=["Operation"]),
            include={
                "FaultEvent",
                "InterfaceReport",
                "Operation",
                "PerformanceBudget",
                "PerformanceResult",
                "StepComparison",
                "SystemDiffResult",
                "SystemMismatch",
            },
        ),
        "migration": _introspect_module(
            __import__("ordeal.migration", fromlist=["migrate"]),
            include={
                "CandidateContract",
                "MigrationChange",
                "MigrationResult",
                "MigrationStage",
                "MutationGate",
                "RegressionArtifacts",
                "migrate",
                "replay_migration_case",
            },
        ),
        "scaling": _introspect_module(
            __import__("ordeal.scaling", fromlist=["scaling"]),
        ),
        "evidence": _introspect_module(
            __import__("ordeal.evidence", fromlist=["evidence"]),
            include={"BugEvidenceVerification", "EvidenceCheck", "verify_bug_evidence"},
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
        )
        + _introspect_module(
            __import__("ordeal.compose", fromlist=["compose"]),
            include={
                "ComposeRunner",
                "ComposeTrace",
                "ComposeReplayReport",
                "ComposeExplorationResult",
                "ComposeRegressionArtifacts",
                "compose_reliability_coverage",
                "measure_compose_workload_strength",
                "build_compose_finding_evidence",
                "run_compose_exploration",
                "replay_compose_trace",
                "save_compose_regression",
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
    annotated = {
        section: _annotate_catalog_section(
            section,
            [dict(item) for item in entries],
        )
        for section, entries in result.items()
    }
    _restore_lazy_entrypoint_collisions()
    return annotated
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
