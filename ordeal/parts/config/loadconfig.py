from __future__ import annotations
# ruff: noqa
# ============================================================================
# Loader
# ============================================================================


def load_config(path: str | Path = "ordeal.toml") -> OrdealConfig:
    """Load and validate an ``ordeal.toml`` file.

    Args:
        path: Path to the TOML file (default: ``ordeal.toml`` in cwd).

    Returns:
        A validated :class:`OrdealConfig`.

    Raises:
        FileNotFoundError: If the file does not exist.
        ConfigError: If the file is invalid.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Config file is not valid UTF-8: {p}: {exc}") from exc

    # Warn on unknown top-level sections
    for key in raw:
        if key not in _KNOWN_SECTIONS:
            raise ConfigError(f"Unknown top-level section: [{key}]")

    # -- Explorer --
    explorer_raw = raw.get("explorer", {})
    _warn_unknown_keys("explorer", explorer_raw, _KNOWN_EXPLORER_KEYS)

    ngram_val = int(explorer_raw.get("ngram", 2))
    if ngram_val < 1:
        raise ConfigError(f"explorer.ngram must be >= 1, got {ngram_val}")

    explorer = ExplorerConfig(
        target_modules=explorer_raw.get("target_modules", []),
        max_time=float(explorer_raw.get("max_time", 60.0)),
        max_runs=explorer_raw.get("max_runs"),
        seed=int(explorer_raw.get("seed", 42)),
        max_checkpoints=int(explorer_raw.get("max_checkpoints", 256)),
        checkpoint_prob=float(explorer_raw.get("checkpoint_prob", 0.4)),
        checkpoint_strategy=explorer_raw.get("checkpoint_strategy", "energy"),
        steps_per_run=int(explorer_raw.get("steps_per_run", 50)),
        fault_toggle_prob=float(explorer_raw.get("fault_toggle_prob", 0.3)),
        workers=int(explorer_raw.get("workers", 0)),
        seed_mutation_respect_strategies=bool(
            explorer_raw.get("seed_mutation_respect_strategies", False)
        ),
        ngram=ngram_val,
        rule_swarm=explorer_raw.get("rule_swarm", False),
        rule_timeout=float(explorer_raw.get("rule_timeout", 30.0)),
    )

    if explorer.checkpoint_strategy not in _VALID_CHECKPOINT_STRATEGIES:
        raise ConfigError(
            f"Invalid checkpoint_strategy: {explorer.checkpoint_strategy!r}. "
            f"Must be one of: {_VALID_CHECKPOINT_STRATEGIES}"
        )

    # -- Tests --
    tests: list[TestConfig] = []
    for i, t in enumerate(raw.get("tests", [])):
        _warn_unknown_keys(f"tests.{i}", t, _KNOWN_TEST_KEYS)
        if "class" not in t:
            raise ConfigError(f"[[tests]] entry {i} is missing required 'class' key")
        tests.append(
            TestConfig(
                class_path=t["class"],
                steps_per_run=t.get("steps_per_run"),
                swarm=t.get("swarm"),
                rule_timeout=(float(t["rule_timeout"]) if "rule_timeout" in t else None),
            )
        )

    # -- Shared fixtures --
    fixtures_raw = raw.get("fixtures", {})
    _warn_unknown_keys("fixtures", fixtures_raw, _KNOWN_FIXTURES_KEYS)
    fixtures = FixturesConfig(
        registries=list(fixtures_raw.get("registries", [])),
    )

    # -- Scan --
    scans: list[ScanConfig] = []
    for i, s in enumerate(raw.get("scan", [])):
        _warn_unknown_keys(f"scan.{i}", s, _KNOWN_SCAN_KEYS)
        if "module" not in s:
            raise ConfigError(f"[[scan]] entry {i} is missing required 'module' key")
        scans.append(
            ScanConfig(
                module=s["module"],
                max_examples=int(s.get("max_examples", 50)),
                mode=str(s.get("mode", "evidence")),
                min_contract_fit=float(s.get("min_contract_fit", 0.55)),
                min_reachability=float(s.get("min_reachability", 0.45)),
                min_realism=float(s.get("min_realism", 0.55)),
                min_fixture_completeness=float(s.get("min_fixture_completeness", 0.55)),
                security_focus=bool(s.get("security_focus", False)),
                require_replayable=bool(s.get("require_replayable", True)),
                proof_bundles=bool(s.get("proof_bundles", True)),
                shell_injection_check=bool(s.get("shell_injection_check", False)),
                seed_from_tests=bool(s.get("seed_from_tests", True)),
                seed_from_fixtures=bool(s.get("seed_from_fixtures", True)),
                seed_from_docstrings=bool(s.get("seed_from_docstrings", True)),
                seed_from_code=bool(s.get("seed_from_code", True)),
                seed_from_call_sites=bool(s.get("seed_from_call_sites", True)),
                treat_any_as_weak=bool(s.get("treat_any_as_weak", True)),
                auto_contracts=list(s.get("auto_contracts", [])),
                ignore_contracts=list(s.get("ignore_contracts", [])),
                targets=list(s.get("targets", [])),
                include_private=bool(s.get("include_private", False)),
                fixtures=s.get("fixtures", {}),
                expected_failures=s.get("expected_failures", []),
                expected_preconditions=_map_of_lists(
                    s.get("expected_preconditions"), key_name="expected_preconditions"
                ),
                fixture_registries=list(s.get("fixture_registries", [])),
                ignore_properties=list(s.get("ignore_properties", [])),
                ignore_relations=list(s.get("ignore_relations", [])),
                expected_properties=_map_of_lists(
                    s.get("expected_properties"), key_name="expected_properties"
                ),
                expected_relations=_map_of_lists(
                    s.get("expected_relations"), key_name="expected_relations"
                ),
                property_overrides=dict(s.get("property_overrides", {})),
                relation_overrides=dict(s.get("relation_overrides", {})),
                contract_overrides=dict(s.get("contract_overrides", {})),
            )
        )
        scan_cfg = scans[-1]
        if scan_cfg.mode not in _VALID_SCAN_MODES:
            raise ConfigError(
                f"Invalid scan.{i}.mode: {scan_cfg.mode!r}. Must be one of: {_VALID_SCAN_MODES}"
            )
        if not (0.0 <= scan_cfg.min_contract_fit <= 1.0):
            raise ConfigError(
                f"scan.{i}.min_contract_fit must be between 0.0 and 1.0, "
                f"got {scan_cfg.min_contract_fit}"
            )
        if not (0.0 <= scan_cfg.min_reachability <= 1.0):
            raise ConfigError(
                f"scan.{i}.min_reachability must be between 0.0 and 1.0, "
                f"got {scan_cfg.min_reachability}"
            )
        if not (0.0 <= scan_cfg.min_realism <= 1.0):
            raise ConfigError(
                f"scan.{i}.min_realism must be between 0.0 and 1.0, got {scan_cfg.min_realism}"
            )
        if not (0.0 <= scan_cfg.min_fixture_completeness <= 1.0):
            raise ConfigError(
                "scan."
                f"{i}.min_fixture_completeness must be between 0.0 and 1.0, "
                f"got {scan_cfg.min_fixture_completeness}"
            )

    # -- Objects --
    object_cfgs: list[ObjectConfig] = []
    for i, obj_raw in enumerate(raw.get("objects", [])):
        _warn_unknown_keys(f"objects.{i}", obj_raw, _KNOWN_OBJECT_KEYS)
        if "target" not in obj_raw:
            raise ConfigError(f"[[objects]] entry {i} is missing required 'target' key")
        object_cfgs.append(
            ObjectConfig(
                target=str(obj_raw["target"]),
                factory=obj_raw.get("factory"),
                setup=obj_raw.get("setup"),
                state_factory=obj_raw.get("state_factory"),
                teardown=obj_raw.get("teardown"),
                harness=str(obj_raw.get("harness", "fresh")),
                scenarios=list(obj_raw.get("scenarios", [])),
                methods=list(obj_raw.get("methods", [])),
                include_private=bool(obj_raw.get("include_private", False)),
            )
        )
        object_cfg = object_cfgs[-1]
        if object_cfg.harness not in _VALID_OBJECT_HARNESSES:
            raise ConfigError(
                f"Invalid objects.{i}.harness: {object_cfg.harness!r}. "
                f"Must be one of: {_VALID_OBJECT_HARNESSES}"
            )

    # -- Contracts --
    contract_cfgs: list[ContractConfig] = []
    for i, contract_raw in enumerate(raw.get("contracts", [])):
        _warn_unknown_keys(f"contracts.{i}", contract_raw, _KNOWN_CONTRACT_KEYS)
        if "target" not in contract_raw:
            raise ConfigError(f"[[contracts]] entry {i} is missing required 'target' key")
        contract_cfgs.append(
            ContractConfig(
                target=str(contract_raw["target"]),
                checks=list(contract_raw.get("checks", [])),
                kwargs=dict(contract_raw.get("kwargs", {})),
                tracked_params=list(contract_raw.get("tracked_params", [])),
                protected_keys=list(contract_raw.get("protected_keys", [])),
                env_param=contract_raw.get("env_param"),
                phase=contract_raw.get("phase"),
                followup_phases=list(contract_raw.get("followup_phases", [])),
                fault=contract_raw.get("fault"),
                handler_name=contract_raw.get("handler_name"),
            )
        )

    # -- Report --
    report_raw = raw.get("report", {})
    _warn_unknown_keys("report", report_raw, _KNOWN_REPORT_KEYS)

    report = ReportConfig(
        format=report_raw.get("format", "text"),
        output=report_raw.get("output", "ordeal-report.json"),
        traces=report_raw.get("traces", False),
        traces_dir=report_raw.get("traces_dir", ".ordeal/traces"),
        verbose=report_raw.get("verbose", explorer_raw.get("verbose", False)),
        corpus_dir=report_raw.get("corpus_dir", ".ordeal/seeds"),
    )

    if report.format not in _VALID_REPORT_FORMATS:
        raise ConfigError(
            f"Invalid report format: {report.format!r}. Must be one of: {_VALID_REPORT_FORMATS}"
        )

    # -- API (optional) --
    api_cfg: APIConfig | None = None

    if "api" in raw:
        a_raw = raw["api"]
        _warn_unknown_keys("api", a_raw, _KNOWN_API_KEYS)
        api_cfg = APIConfig(
            schema_url=a_raw.get("schema_url"),
            app=a_raw.get("app"),
            wsgi=a_raw.get("wsgi", False),
            schema_path=a_raw.get("schema_path", "/openapi.json"),
            base_url=a_raw.get("base_url"),
            faults=a_raw.get("faults", []),
            fault_probability=float(a_raw.get("fault_probability", 0.3)),
            seed=int(a_raw.get("seed", 42)),
            swarm=a_raw.get("swarm", False),
            max_examples=int(a_raw.get("max_examples", 100)),
            headers=a_raw.get("headers", {}),
        )

    # -- Mutations (optional) --
    mutations_cfg: MutationConfig | None = None
    if "mutations" in raw:
        m_raw = raw["mutations"]
        _warn_unknown_keys("mutations", m_raw, _KNOWN_MUTATIONS_KEYS)

        m_preset = m_raw.get("preset")
        m_operators = m_raw.get("operators")

        if m_preset is not None and m_operators is not None:
            raise ConfigError(
                "Cannot specify both 'preset' and 'operators' in [mutations]. "
                "Use one or the other."
            )
        if m_preset is not None and m_preset not in _valid_presets():
            raise ConfigError(
                f"Invalid mutations preset: {m_preset!r}. Must be one of: {_valid_presets()}"
            )

        m_threshold = float(m_raw.get("threshold", 0.0))
        if not (0.0 <= m_threshold <= 1.0):
            raise ConfigError(
                f"mutations.threshold must be between 0.0 and 1.0, got {m_threshold}"
            )

        mutations_cfg = MutationConfig(
            targets=m_raw.get("targets", []),
            preset=m_preset if m_preset is not None else "standard",
            operators=m_operators,
            workers=int(m_raw.get("workers", 1)),
            threshold=m_threshold,
            filter_equivalent=m_raw.get("filter_equivalent", True),
            equivalence_samples=int(m_raw.get("equivalence_samples", 10)),
            test_filter=m_raw.get("test_filter"),
            mutant_timeout=float(mt) if (mt := m_raw.get("mutant_timeout")) is not None else None,
            promote_clusters_only=bool(m_raw.get("promote_clusters_only", True)),
            cluster_min_size=int(m_raw.get("cluster_min_size", 2)),
        )
        if mutations_cfg.cluster_min_size < 1:
            raise ConfigError(
                f"mutations.cluster_min_size must be >= 1, got {mutations_cfg.cluster_min_size}"
            )

    # -- Audit --
    audit_raw = raw.get("audit", {})
    _warn_unknown_keys("audit", audit_raw, _KNOWN_AUDIT_KEYS)
    audit_targets_raw = audit_raw.get("targets", [])
    audit_targets: list[AuditTargetConfig] = []
    for i, target_raw in enumerate(audit_targets_raw):
        _warn_unknown_keys(f"audit.targets.{i}", target_raw, _KNOWN_AUDIT_TARGET_KEYS)
        if "target" not in target_raw:
            raise ConfigError(f"[[audit.targets]] entry {i} is missing required 'target' key")
        audit_targets.append(
            AuditTargetConfig(
                target=str(target_raw["target"]),
                factory=target_raw.get("factory"),
                setup=target_raw.get("setup"),
                state_factory=target_raw.get("state_factory"),
                teardown=target_raw.get("teardown"),
                harness=str(target_raw.get("harness", "fresh")),
                scenarios=list(target_raw.get("scenarios", [])),
                methods=list(target_raw.get("methods", [])),
                include_private=bool(target_raw.get("include_private", False)),
            )
        )
        target_cfg = audit_targets[-1]
        if target_cfg.harness not in _VALID_OBJECT_HARNESSES:
            raise ConfigError(
                f"Invalid audit.targets.{i}.harness: {target_cfg.harness!r}. "
                f"Must be one of: {_VALID_OBJECT_HARNESSES}"
            )
    audit_cfg = AuditConfig(
        modules=list(audit_raw.get("modules", [])),
        targets=audit_targets,
        test_dir=audit_raw.get("test_dir", "tests"),
        max_examples=int(audit_raw.get("max_examples", 20)),
        workers=int(audit_raw.get("workers", 1)),
        validation_mode=audit_raw.get("validation_mode", "fast"),
        min_fixture_completeness=float(audit_raw.get("min_fixture_completeness", 0.0)),
        show_generated=bool(audit_raw.get("show_generated", False)),
        save_generated=audit_raw.get("save_generated"),
        write_gaps_dir=audit_raw.get("write_gaps_dir"),
        include_exploratory_function_gaps=bool(
            audit_raw.get("include_exploratory_function_gaps", False)
        ),
        require_direct_tests=bool(audit_raw.get("require_direct_tests", False)),
    )
    if audit_cfg.validation_mode not in _VALID_AUDIT_VALIDATION_MODES:
        raise ConfigError(
            f"Invalid audit.validation_mode: {audit_cfg.validation_mode!r}. "
            f"Must be one of: {_VALID_AUDIT_VALIDATION_MODES}"
        )
    if not (0.0 <= audit_cfg.min_fixture_completeness <= 1.0):
        raise ConfigError(
            "audit.min_fixture_completeness must be between 0.0 and 1.0, "
            f"got {audit_cfg.min_fixture_completeness}"
        )

    # -- Init --
    init_raw = raw.get("init", {})
    _warn_unknown_keys("init", init_raw, _KNOWN_INIT_KEYS)
    init_cfg = InitConfig(
        target=init_raw.get("target"),
        output_dir=init_raw.get("output_dir", "tests"),
        ci=bool(init_raw.get("ci", False)),
        ci_name=init_raw.get("ci_name", "ordeal"),
        install_skill=bool(init_raw.get("install_skill", False)),
        close_gaps=bool(init_raw.get("close_gaps", False)),
        gap_output_dir=init_raw.get("gap_output_dir"),
        mutation_preset=init_raw.get("mutation_preset", "essential"),
        scan_max_examples=int(init_raw.get("scan_max_examples", 10)),
    )
    if init_cfg.mutation_preset not in _valid_presets():
        raise ConfigError(
            f"Invalid init.mutation_preset: {init_cfg.mutation_preset!r}. "
            f"Must be one of: {_valid_presets()}"
        )

    # -- Diff --
    diff_raw = raw.get("diff", {})
    _warn_unknown_keys("diff", diff_raw, _KNOWN_DIFF_KEYS)
    diff_cfg = DiffConfig(
        target=diff_raw.get("target"),
        base_ref=diff_raw.get("base_ref"),
        candidate_ref=str(diff_raw.get("candidate_ref", "HEAD")),
        max_examples=int(diff_raw.get("max_examples", 100)),
        seed=int(diff_raw.get("seed", 42)),
        rtol=(float(value) if (value := diff_raw.get("rtol")) is not None else None),
        atol=(float(value) if (value := diff_raw.get("atol")) is not None else None),
        include_private=bool(diff_raw.get("include_private", False)),
        fixture_registries=list(diff_raw.get("fixture_registries", [])),
        replay_attempts=int(diff_raw.get("replay_attempts", 2)),
        save_artifacts=bool(diff_raw.get("save_artifacts", False)),
        artifact_dir=str(diff_raw.get("artifact_dir", ".ordeal/diff")),
    )
    if diff_cfg.max_examples < 1:
        raise ConfigError("diff.max_examples must be >= 1")
    if diff_cfg.rtol is not None and diff_cfg.rtol < 0:
        raise ConfigError("diff.rtol must be >= 0")
    if diff_cfg.atol is not None and diff_cfg.atol < 0:
        raise ConfigError("diff.atol must be >= 0")
    if diff_cfg.replay_attempts < 1:
        raise ConfigError("diff.replay_attempts must be >= 1")
    if not diff_cfg.candidate_ref.strip():
        raise ConfigError("diff.candidate_ref cannot be empty")
    if not diff_cfg.artifact_dir.strip():
        raise ConfigError("diff.artifact_dir cannot be empty")

    compose_cfg = _load_compose_config(raw["compose"], config_path=p) if "compose" in raw else None

    return OrdealConfig(
        explorer=explorer,
        tests=tests,
        fixtures=fixtures,
        scan=scans,
        objects=object_cfgs,
        contracts=contract_cfgs,
        report=report,
        api=api_cfg,
        mutations=mutations_cfg,
        audit=audit_cfg,
        init=init_cfg,
        diff=diff_cfg,
        compose=compose_cfg,
    )
