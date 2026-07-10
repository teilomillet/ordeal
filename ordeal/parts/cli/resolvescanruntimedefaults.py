from __future__ import annotations
# ruff: noqa
def _resolve_scan_runtime_defaults(
    target: str,
    *,
    requested_examples: int,
    allow_config_override: bool = False,
    resolve_config_imports: bool = True,
) -> ScanRuntimeDefaults:
    """Load fixture registries and optional ``[[scan]]`` defaults for *target*."""
    module_name = _scan_base_module(target)
    warnings: list[str] = []

    values: dict[str, Any] = {
        "max_examples": requested_examples,
        "mode": "evidence",
        "seed_from_tests": True,
        "seed_from_fixtures": True,
        "seed_from_docstrings": True,
        "seed_from_code": True,
        "seed_from_call_sites": True,
        "treat_any_as_weak": True,
        "proof_bundles": True,
        "require_replayable": True,
        "shell_injection_check": False,
        "auto_contracts": [],
        "min_contract_fit": 0.55,
        "min_reachability": 0.45,
        "min_realism": 0.55,
        "min_fixture_completeness": _DEFAULT_SCAN_MIN_FIXTURE_COMPLETENESS,
        "security_focus": False,
        "fixtures": None,
        "targets": [],
        "include_private": False,
        "object_factories": None,
        "object_setups": None,
        "object_scenarios": None,
        "object_state_factories": None,
        "object_teardowns": None,
        "object_harnesses": None,
        "contract_checks": {},
        "expected_failures": [],
        "expected_preconditions": {},
        "ignore_contracts": [],
        "ignore_properties": [],
        "ignore_relations": [],
        "contract_overrides": {},
        "expected_properties": {},
        "expected_relations": {},
        "property_overrides": {},
        "relation_overrides": {},
    }

    def _build_defaults() -> ScanRuntimeDefaults:
        return ScanRuntimeDefaults(
            max_examples=int(values["max_examples"]),
            mode=str(values["mode"]),
            seed_from_tests=bool(values["seed_from_tests"]),
            seed_from_fixtures=bool(values["seed_from_fixtures"]),
            seed_from_docstrings=bool(values["seed_from_docstrings"]),
            seed_from_code=bool(values["seed_from_code"]),
            seed_from_call_sites=bool(values["seed_from_call_sites"]),
            treat_any_as_weak=bool(values["treat_any_as_weak"]),
            proof_bundles=bool(values["proof_bundles"]),
            require_replayable=bool(values["require_replayable"]),
            shell_injection_check=bool(values["shell_injection_check"]),
            auto_contracts=list(values["auto_contracts"]),
            min_contract_fit=float(values["min_contract_fit"]),
            min_reachability=float(values["min_reachability"]),
            min_realism=float(values["min_realism"]),
            min_fixture_completeness=float(values["min_fixture_completeness"]),
            security_focus=bool(values["security_focus"]),
            fixtures=values["fixtures"],
            targets=list(values["targets"]),
            include_private=bool(values["include_private"]),
            object_factories=values["object_factories"],
            object_setups=values["object_setups"],
            object_scenarios=values["object_scenarios"],
            object_state_factories=values["object_state_factories"],
            object_teardowns=values["object_teardowns"],
            object_harnesses=values["object_harnesses"],
            contract_checks=dict(values["contract_checks"]),
            expected_failures=list(values["expected_failures"]),
            expected_preconditions={
                str(name): list(items)
                for name, items in dict(values["expected_preconditions"]).items()
            },
            registry_warnings=list(warnings),
            ignore_contracts=list(values["ignore_contracts"]),
            ignore_properties=list(values["ignore_properties"]),
            ignore_relations=list(values["ignore_relations"]),
            contract_overrides={
                str(name): list(items)
                for name, items in dict(values["contract_overrides"]).items()
            },
            expected_properties={
                str(name): list(items)
                for name, items in dict(values["expected_properties"]).items()
            },
            expected_relations={
                str(name): list(items)
                for name, items in dict(values["expected_relations"]).items()
            },
            property_overrides={
                str(name): list(items)
                for name, items in dict(values["property_overrides"]).items()
            },
            relation_overrides={
                str(name): list(items)
                for name, items in dict(values["relation_overrides"]).items()
            },
        )

    config_path = Path("ordeal.toml")
    if not config_path.exists():
        return _build_defaults()

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ConfigError):
        if resolve_config_imports:
            warnings.extend(_load_fixture_registry_warnings())
        return _build_defaults()

    if resolve_config_imports:
        warnings.extend(_load_fixture_registry_warnings(shared_modules=cfg.fixtures.registries))
    shared_object_specs = _config_object_specs_for_module(cfg, module_name)
    try:
        (
            values["object_factories"],
            values["object_setups"],
            values["object_scenarios"],
            values["object_state_factories"],
            values["object_teardowns"],
            values["object_harnesses"],
        ) = _object_runtime_maps(
            shared_object_specs,
            resolve_imports=resolve_config_imports,
        )
    except Exception as exc:
        warnings.append(f"object factory config failed for {module_name}: {exc}")
        (
            values["object_factories"],
            values["object_setups"],
            values["object_scenarios"],
        ) = ({}, {}, {})
        (
            values["object_state_factories"],
            values["object_teardowns"],
            values["object_harnesses"],
        ) = (
            {},
            {},
            {},
        )
    try:
        values["contract_checks"] = _config_contract_checks_for_module(
            cfg,
            module_name,
            resolve_imports=resolve_config_imports,
        )
    except Exception as exc:
        warnings.append(f"contract config failed for {module_name}: {exc}")
        values["contract_checks"] = {}

    match = next((entry for entry in cfg.scan if entry.module == module_name), None)
    if match is None:
        if not resolve_config_imports:
            warnings.extend(
                _safe_listing_config_warning(
                    has_fixture_registries=bool(cfg.fixtures.registries),
                    has_object_hooks=bool(shared_object_specs),
                    has_contracts=bool(cfg.contracts),
                )
            )
        return _build_defaults()

    if allow_config_override:
        values["max_examples"] = match.max_examples
    values["mode"] = match.mode
    values["seed_from_tests"] = bool(match.seed_from_tests)
    values["seed_from_fixtures"] = bool(match.seed_from_fixtures)
    values["seed_from_docstrings"] = bool(match.seed_from_docstrings)
    values["seed_from_code"] = bool(match.seed_from_code)
    values["seed_from_call_sites"] = bool(match.seed_from_call_sites)
    values["treat_any_as_weak"] = bool(match.treat_any_as_weak)
    values["proof_bundles"] = bool(match.proof_bundles)
    values["require_replayable"] = bool(match.require_replayable)
    values["shell_injection_check"] = bool(getattr(match, "shell_injection_check", False))
    values["auto_contracts"] = list(match.auto_contracts)
    values["min_contract_fit"] = float(match.min_contract_fit)
    values["min_reachability"] = float(match.min_reachability)
    values["min_realism"] = float(getattr(match, "min_realism", 0.55))
    values["min_fixture_completeness"] = float(
        getattr(match, "min_fixture_completeness", _DEFAULT_SCAN_MIN_FIXTURE_COMPLETENESS)
    )
    values["security_focus"] = bool(getattr(match, "security_focus", False))
    values["targets"] = list(match.targets)
    values["include_private"] = bool(match.include_private)
    values["fixtures"] = _parse_scan_fixture_specs(match.fixtures)
    values["expected_failures"] = list(match.expected_failures)
    values["expected_preconditions"] = {
        str(name): list(items) for name, items in match.expected_preconditions.items()
    }
    if resolve_config_imports:
        warnings.extend(_load_fixture_registry_warnings(extra_modules=match.fixture_registries))
    values["ignore_contracts"] = list(match.ignore_contracts)
    values["ignore_properties"] = list(match.ignore_properties)
    values["ignore_relations"] = list(match.ignore_relations)
    values["contract_overrides"] = {
        str(name): list(items) for name, items in match.contract_overrides.items()
    }
    values["expected_properties"] = {
        str(name): list(items) for name, items in match.expected_properties.items()
    }
    values["expected_relations"] = {
        str(name): list(items) for name, items in match.expected_relations.items()
    }
    values["property_overrides"] = {
        str(name): list(items) for name, items in match.property_overrides.items()
    }
    values["relation_overrides"] = {
        str(name): list(items) for name, items in match.relation_overrides.items()
    }
    if not resolve_config_imports:
        warnings.extend(
            _safe_listing_config_warning(
                has_fixture_registries=bool(cfg.fixtures.registries or match.fixture_registries),
                has_object_hooks=bool(shared_object_specs),
                has_contracts=bool(cfg.contracts),
            )
        )
    return _build_defaults()
def _resolve_check_runtime_defaults(
    target: str,
    *,
    config_path: str | None = None,
) -> CheckRuntimeDefaults:
    """Load config-backed object hooks and explicit contracts for *target*."""
    module_name = _target_module_name(target)
    warnings: list[str] = []
    config = _load_optional_config(config_path)
    if config is None:
        return CheckRuntimeDefaults()

    try:
        object_specs = _config_object_specs_for_module(config, module_name)
        (
            object_factories,
            object_setups,
            object_scenarios,
            object_state_factories,
            object_teardowns,
            object_harnesses,
        ) = _object_runtime_maps(object_specs)
    except Exception as exc:
        warnings.append(f"object config failed for {module_name}: {exc}")
        object_factories, object_setups, object_scenarios = {}, {}, {}
        object_state_factories, object_teardowns, object_harnesses = {}, {}, {}

    try:
        contract_checks = _config_contract_checks_for_module(config, module_name)
    except Exception as exc:
        warnings.append(f"contract config failed for {module_name}: {exc}")
        contract_checks = {}

    display_name = _scan_display_name(module_name, target)
    return CheckRuntimeDefaults(
        object_factories=object_factories,
        object_setups=object_setups,
        object_scenarios=object_scenarios,
        object_state_factories=object_state_factories,
        object_teardowns=object_teardowns,
        object_harnesses=object_harnesses,
        contract_checks=list(contract_checks.get(display_name, [])),
        registry_warnings=warnings,
    )
def _check_contract_names(args: argparse.Namespace) -> list[str]:
    """Normalize repeated ``--contract`` CLI values."""
    names: list[str] = []
    for raw in list(getattr(args, "contract", []) or []):
        name = str(raw).strip()
        if name and name not in names:
            names.append(name)
    return names
def _scan_target_selectors(args: argparse.Namespace) -> list[str]:
    """Normalize repeated ``--target`` selector values for ``scan``."""
    selectors: list[str] = []
    for raw in list(getattr(args, "scan_targets", []) or []):
        selector = str(raw).strip()
        if selector and selector not in selectors:
            selectors.append(selector)
    return selectors
_BROAD_PACKAGE_SCAN_DEFAULT_MAX_EXAMPLES = 5
_PACKAGE_ROOT_ORCHESTRATION_PREFIXES = (
    "audit",
    "benchmark",
    "chaos_for",
    "explore",
    "fuzz",
    "mine",
    "mutate",
    "replay",
    "scan",
    "verify",
)
_PACKAGE_ROOT_HELPER_NAMES = {
    "auto_configure",
    "always",
    "bounded",
    "catalog",
    "declare",
    "finite",
    "reachable",
    "report",
    "sometimes",
    "unreachable",
}
_PACKAGE_ROOT_HEAVY_MODULE_PREFIXES = (
    "ordeal.audit",
    "ordeal.cli",
    "ordeal.explore",
    "ordeal.mine",
    "ordeal.mutations",
    "ordeal.scaling",
    "ordeal.state",
    "ordeal.trace",
)
def _is_package_module(module_name: str) -> bool:
    """Return whether *module_name* resolves to a package root module."""
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return False
    if getattr(mod, "__path__", None):
        return True
    module_file = getattr(mod, "__file__", "") or ""
    return Path(module_file).name == "__init__.py"
def _package_root_harness_penalty(row: Mapping[str, Any]) -> float:
    """Return the support-helper penalty for one broad package-root target."""
    name = str(row.get("name", "")).strip().lower()
    source_path = str(row.get("source_path") or "").strip().lower().replace("\\", "/")
    lifecycle_phase = str(row.get("lifecycle_phase") or "").strip().lower()
    penalty = 0.0
    if name.startswith(("make_", "prime_", "setup_", "cleanup_", "teardown_", "scenario_")):
        penalty += 12.0
    if any(token in name for token in ("fixture", "factory", "scenario")):
        penalty += 10.0
    if any(token in name for token in ("setup", "cleanup", "teardown")):
        penalty += 8.0
    if lifecycle_phase in {"setup", "cleanup", "teardown"}:
        penalty += 8.0
    if source_path.endswith("conftest.py"):
        penalty += 10.0
    if source_path.startswith("tests/") or "/tests/" in source_path:
        penalty += 6.0
    if any(token in source_path for token in ("support_factory", "support_factories", "fixtures")):
        penalty += 6.0
    return penalty
def _package_root_scan_priority(row: Mapping[str, Any]) -> tuple[float, str, str]:
    """Return a descending priority key for representative package-root scan targets."""
    name = str(row.get("name", "")).strip()
    source_module = str(row.get("source_module") or row.get("module") or "")
    score = 0.0
    if bool(row.get("runnable", False)):
        score += 50.0
    if not bool(row.get("factory_required", False)):
        score += 8.0
    if bool(row.get("factory_configured", False)):
        score += 2.0
    if str(row.get("kind", "")) == "function":
        score += 2.0
    if str(row.get("async", "")) == "async":
        score -= 1.0
    if name in _PACKAGE_ROOT_HELPER_NAMES:
        score += 10.0
    if name in {"chaos_test"}:
        score += 4.0
    if any(name.startswith(prefix) for prefix in _PACKAGE_ROOT_ORCHESTRATION_PREFIXES) or name in {
        "generate_starter_tests",
        "init_project",
    }:
        score -= 12.0
    if source_module.startswith(_PACKAGE_ROOT_HEAVY_MODULE_PREFIXES):
        score -= 6.0
    if any(
        token in name
        for token in (
            "classify",
            "extract",
            "filter",
            "prove",
            "fit",
            "analyze",
            "validate",
        )
    ):
        score += 6.0
    if source_module.startswith("hypothesis_temporary_module_"):
        score -= 20.0
    if name.startswith("register_"):
        score -= 8.0
    if name == "builtin_contract_check":
        score -= 10.0
    if name.endswith("_contract"):
        # Contract-builder exports are useful when targeted explicitly, but
        # broad package-root scans should prefer concrete implementation helpers.
        score -= 4.0
    if name.endswith("_strategy"):
        score -= 6.0
    if name in {"activate", "deactivate", "set_seed"}:
        score -= 4.0
    score -= _package_root_harness_penalty(row)
    return (-score, source_module, name)
def _build_explicit_contract_checks(func: Any, names: Sequence[str]) -> list[Any]:
    """Build direct built-in contract checks for one resolved callable."""
    from ordeal.auto import (
        _boundary_smoke_inputs,
        _expand_contract_names_ordered,
        _unwrap,
        builtin_contract_check,
    )

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        tracked_params: list[str] = []
    else:
        tracked_params = [
            param.name for param in sig.parameters.values() if param.name not in {"self", "cls"}
        ]

    phase = (
        str(getattr(func, "__ordeal_lifecycle_phase__", "") or "").strip()
        or str(getattr(_unwrap(func), "__ordeal_lifecycle_phase__", "") or "").strip()
        or None
    )
    smoke_inputs = _boundary_smoke_inputs(func)
    base_kwargs = dict(smoke_inputs[0]) if smoke_inputs else {}
    followup_phases = ("cleanup", "teardown") if phase in {"setup", "rollout"} else None
    checks: list[Any] = []
    for name in _expand_contract_names_ordered(names):
        checks.append(
            builtin_contract_check(
                name,
                kwargs=dict(base_kwargs),
                tracked_params=tracked_params,
                phase=phase,
                followup_phases=followup_phases,
            )
        )
    return checks
def _target_harness_mode(
    target: str,
    object_harnesses: Mapping[str, str] | None,
) -> str | None:
    """Resolve one configured harness mode for a scan/audit/mutation target."""
    if not object_harnesses:
        return None
    module_name = _target_module_name(target)
    display_name = _scan_display_name(module_name, target)
    if "." not in display_name:
        return None
    owner_name = display_name.rsplit(".", 1)[0]
    return object_harnesses.get(f"{module_name}:{owner_name}")
def _mutation_contract_context_for_target(
    cfg: OrdealConfig | None,
    target: str,
) -> dict[str, Any]:
    """Build mutation-ranking contract metadata for one configured target."""
    if cfg is None:
        return {}

    from ordeal.mutations import mutation_contract_context

    module_name = _target_module_name(target)
    try:
        checks_by_target = _config_contract_checks_for_module(cfg, module_name)
    except Exception:
        checks_by_target = {}
    try:
        object_specs = _config_object_specs_for_module(cfg, module_name)
        *_unused, object_harnesses = _object_runtime_maps(object_specs)
    except Exception:
        object_harnesses = {}
    display_name = _scan_display_name(module_name, target)
    return mutation_contract_context(
        checks_by_target.get(display_name, ()),
        harness=_target_harness_mode(target, object_harnesses),
    )
