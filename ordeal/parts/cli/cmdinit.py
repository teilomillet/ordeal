from __future__ import annotations
# ruff: noqa
def _cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap test files for untested modules."""
    import re
    import subprocess

    from ordeal.audit import audit
    from ordeal.mutations import init_project

    try:
        cfg = _load_optional_config(getattr(args, "config", None))
    except FileNotFoundError:
        _stderr(f"Config not found: {args.config}\n")
        return 1
    except ConfigError as e:
        _stderr(f"Config error: {e}\n")
        return 1

    init_cfg = cfg.init if cfg is not None else None
    audit_cfg = cfg.audit if cfg is not None else None

    target_value = _cli_or_config(args.target, init_cfg.target if init_cfg else None)
    target: str | None = target_value or None
    output_dir = str(_cli_or_config(args.output_dir, init_cfg.output_dir if init_cfg else "tests"))
    dry_run: bool = args.dry_run
    ci = bool(_cli_or_config(args.ci, init_cfg.ci if init_cfg else False))
    ci_name = str(_cli_or_config(args.ci_name, init_cfg.ci_name if init_cfg else "ordeal"))
    install_skill = bool(
        _cli_or_config(args.install_skill, init_cfg.install_skill if init_cfg else False)
    )
    close_gaps = bool(_cli_or_config(args.close_gaps, init_cfg.close_gaps if init_cfg else False))
    gap_output_dir = str(
        _cli_or_config(
            None,
            init_cfg.gap_output_dir if init_cfg and init_cfg.gap_output_dir else output_dir,
        )
    )
    init_mutation_preset = str(
        _cli_or_config(None, init_cfg.mutation_preset if init_cfg else "essential")
    )
    init_scan_max_examples = int(
        _cli_or_config(None, init_cfg.scan_max_examples if init_cfg else 10)
    )
    close_gap_max_examples = audit_cfg.max_examples if audit_cfg is not None else 10
    close_gap_workers = audit_cfg.workers if audit_cfg is not None else 1
    close_gap_validation_mode = audit_cfg.validation_mode if audit_cfg is not None else "fast"
    close_gap_include_exploratory = bool(
        audit_cfg.include_exploratory_function_gaps if audit_cfg is not None else False
    )
    ci_workflow_path: Path | None = None
    if ci:
        try:
            ci_workflow_path = _workflow_path_from_ci_name(ci_name)
        except ValueError as exc:
            _stderr(f"Invalid CI workflow name: {exc}\n")
            return 2
    try:
        output_dir = str(_workspace_output_path(output_dir, label="init.output_dir"))
        if close_gaps:
            gap_output_dir = str(
                _workspace_output_path(gap_output_dir, label="init.gap_output_dir")
            )
    except ValueError as exc:
        _stderr(f"Invalid init output path: {exc}\n")
        return 2

    results = init_project(target=target, output_dir=output_dir, dry_run=dry_run)

    if not results:
        if target:
            _stderr(f"Could not resolve {target!r}. Is it importable?\n")
        else:
            _stderr(
                "No Python package found in the current directory.\n  Usage: ordeal init myapp\n"
            )
        return 1

    pkg = target or results[0]["module"].split(".")[0]

    generated = [r for r in results if r["status"] == "generated"]
    existed = sum(1 for r in results if r["status"] == "exists")

    # --- CI workflow ---
    ci_path: str | None = None
    ci_content: str | None = None
    if ci and ci_workflow_path is not None:
        ci_path = _display_path(ci_workflow_path)
        ci_content = _generate_ci_workflow(pkg)

    # --- Install AI skill ---
    skill_path = _install_skill(dry_run=dry_run) if install_skill else None

    if dry_run:
        _stderr(f"\nordeal init — DRY RUN for {pkg}\n\n")
        for r in generated:
            print(f"\n# --- {r['path']} ---\n")
            print(r["content"])
        if ci_content:
            print(f"\n# --- {ci_path} ---\n")
            print(ci_content)
        n_files = len(generated) + (1 if ci_content else 0) + (1 if skill_path else 0)
        _stderr(f"  Would generate {n_files} file(s)\n\n")
        return 0

    if not generated and not ci:
        _stderr(f"\nordeal init — {pkg}: all modules already have tests.\n\n")
        return 0

    # --- Write CI workflow ---
    if ci_workflow_path is not None and ci_content:
        ci_p = ci_workflow_path
        ci_p.parent.mkdir(parents=True, exist_ok=True)
        ci_p.write_text(ci_content, encoding="utf-8")

    if not generated:
        _stderr(f"\nordeal init — {pkg}: all modules already have tests.\n")
        _stderr(f"  Generated: {ci_path}\n\n")
        return 0

    # --- Setup subprocess env ---
    env = dict(os.environ)
    cwd = os.getcwd()
    pypath = env.get("PYTHONPATH", "")
    src = os.path.join(cwd, "src")
    extra = src if os.path.isdir(src) else cwd
    env["PYTHONPATH"] = f"{extra}:{pypath}" if pypath else extra

    def _run_ordeal(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from ordeal.cli import main; import sys; sys.exit(main(" + repr(argv) + "))",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    def _aggregate_mutation_score(results: Sequence[Any]) -> str:
        counts = [result.mutation_score_counts for result in results]
        concrete = [count for count in counts if count is not None]
        if not concrete:
            return ""
        killed = sum(count[0] for count in concrete)
        total = sum(count[1] for count in concrete)
        if total <= 0:
            return ""
        return f"{killed}/{total} ({(killed / total):.0%})"

    # --- Phase 1: Verify generated tests pass ---
    test_files = [r["path"] for r in generated]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line", "--no-header", *test_files],
        capture_output=True,
        text=True,
    )
    tests_pass = proc.returncode == 0

    # --- Phase 2: Mutation loop ---
    # Collect function-level targets for more reliable mutation testing
    mut_targets: list[str] = []
    for r in generated:
        content = r.get("content", "")
        mod = r["module"]
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"def test_{mod.replace('.', '_')}_") and "_pinned" in stripped:
                prefix = f"def test_{mod.replace('.', '_')}_"
                func = stripped.split("_pinned")[0].replace(prefix, "")
                mut_targets.append(f"{mod}.{func}")
    if not mut_targets:
        mut_targets = [r["module"] for r in generated]

    mutation_score = ""
    gap_stub_files: list[dict[str, Any]] = []
    weakest_tests: list[dict[str, Any]] = []
    if close_gaps:
        generated_modules = [r["module"] for r in generated]
        audit_target_specs_by_module: dict[str, dict[str, Any]] = {}

        def _merge_target_spec(spec: Any) -> None:
            target = str(getattr(spec, "target"))
            base_module = _scan_base_module(target)
            bucket = audit_target_specs_by_module.setdefault(base_module, {})
            existing = bucket.get(target)
            if existing is None:
                bucket[target] = spec
                return
            from types import SimpleNamespace

            bucket[target] = SimpleNamespace(
                target=target,
                factory=getattr(spec, "factory", None) or getattr(existing, "factory", None),
                setup=getattr(spec, "setup", None) or getattr(existing, "setup", None),
                methods=list(
                    getattr(spec, "methods", None) or getattr(existing, "methods", []) or []
                ),
                include_private=bool(
                    getattr(spec, "include_private", False)
                    or getattr(existing, "include_private", False)
                ),
            )

        if cfg is not None:
            for spec in cfg.objects:
                _merge_target_spec(spec)
        if audit_cfg is not None:
            for spec in audit_cfg.targets:
                _merge_target_spec(spec)
        normalized_audit_specs = {
            module: list(specs.values()) for module, specs in audit_target_specs_by_module.items()
        }
        audit_results = [
            audit(
                module,
                **(
                    {
                        "targets": normalized_audit_specs[module],
                        "test_dir": output_dir,
                        "max_examples": close_gap_max_examples,
                        "workers": close_gap_workers,
                        "validation_mode": close_gap_validation_mode,
                    }
                    if module in normalized_audit_specs
                    else {
                        "test_dir": output_dir,
                        "max_examples": close_gap_max_examples,
                        "workers": close_gap_workers,
                        "validation_mode": close_gap_validation_mode,
                    }
                ),
            )
            for module in generated_modules
        ]
        mutation_score = _aggregate_mutation_score(audit_results)
        gap_stub_files = _write_audit_gap_stubs(
            audit_results,
            output_dir=gap_output_dir,
            include_exploratory_function_gaps=close_gap_include_exploratory,
        )
        weakest_tests = [
            {"module": result.module, **item}
            for result in audit_results
            for item in result.weakest_tests
        ]
    else:
        mp = _run_ordeal(["mutate", *mut_targets, "-p", init_mutation_preset])
        for line in mp.stdout.splitlines():
            if line.startswith("Score:"):
                mutation_score = line.strip()
                break

    # --- Phase 3: Lightweight read-only scan ---
    initial_scan = _run_init_scan(
        [r["module"] for r in generated],
        max_examples=init_scan_max_examples,
    )

    # --- Count what was generated ---
    n_tests = 0
    n_pinned = 0
    n_properties = 0
    n_chaos = 0
    pinned_values: list[str] = []
    property_names: list[str] = []

    for r in generated:
        content = r.get("content", "")
        # Re-read in case mutation loop appended stubs
        if r["path"] and Path(r["path"]).exists():
            content = Path(r["path"]).read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("def test_"):
                n_tests += 1
                if "_pinned" in stripped:
                    n_pinned += 1
                elif "_properties" in stripped:
                    n_properties += 1
            if "chaos_for(" in stripped:
                n_chaos += 1
            # Collect pinned values for review
            if stripped.startswith("assert ") and "==" in stripped:
                expr = stripped.removeprefix("assert ").strip()
                skip_kw = (
                    "isinstance",
                    "is not",
                    "is None",
                    "len(",
                    "math.",
                    "not ",
                    ">= 0",
                    "== result",
                    "<= result",
                    "== ...",
                )
                if not any(kw in expr for kw in skip_kw):
                    if "pytest.approx(" in expr:
                        expr = re.sub(r"pytest\.approx\(([^)]+)\)", r"\1", expr)
                    pinned_values.append(expr)
            # Collect discovered properties
            if stripped.startswith('"""Discovered:'):
                props = stripped.replace('"""Discovered:', "").rstrip('."""')
                property_names.extend(p.strip() for p in props.split(","))

    # Deduplicate properties
    seen: set[str] = set()
    unique_props: list[str] = []
    for p in property_names:
        if p not in seen:
            seen.add(p)
            unique_props.append(p)

    # --- Print the quality report ---
    _stderr(f"\n{'=' * 60}\n")
    _stderr(f"  ordeal init — quality report for {pkg}\n")
    _stderr(f"{'=' * 60}\n\n")

    _stderr(f"  Scanned:    {len(results)} module(s)")
    if existed:
        _stderr(f" ({existed} already tested)")
    _stderr("\n")

    _stderr(f"  Generated:  {n_tests} tests")
    parts = []
    if n_pinned:
        parts.append(f"{n_pinned} pinned")
    if n_properties:
        parts.append(f"{n_properties} property")
    if n_chaos:
        parts.append(f"{n_chaos} chaos")
    if parts:
        _stderr(f" ({', '.join(parts)})")
    _stderr("\n")

    if unique_props:
        _stderr(f"  Properties: {len(unique_props)} discovered")
        # Show the interesting ones
        interesting = [
            p
            for p in unique_props
            if not p.startswith("output type")
            and p not in ("deterministic", "never None", "no NaN")
        ]
        if interesting:
            _stderr(f" — {', '.join(interesting[:5])}")
        _stderr("\n")

    _stderr(f"  Tests pass: {'yes' if tests_pass else 'NO — check generated files'}\n")

    if mutation_score and "0/0" not in mutation_score:
        _stderr(f"  Mutations:  {mutation_score.removeprefix('Score: ')}\n")
        if not close_gaps:
            _stderr(
                "  Gaps:       report-only (use --close-gaps to write draft audit stub files)\n"
            )
    if close_gaps and gap_stub_files:
        _stderr(f"  Gaps:       wrote {len(gap_stub_files)} draft stub file(s) from audit\n")
    if close_gaps and weakest_tests:
        preview = ", ".join(
            f"{item['test']} ({item['kills']} kill(s))" for item in weakest_tests[:3]
        )
        _stderr(f"  Weakest:    {preview}\n")

    if initial_scan["status"] in {"findings found", "exploratory findings"}:
        _stderr(
            "  Initial scan:"
            f" {len(initial_scan['findings'])} finding(s)"
            f" across {len(initial_scan['modules'])} module(s)\n"
        )
        for finding in initial_scan["findings"][:3]:
            _stderr(f"    {finding['summary']}\n")
        remaining = len(initial_scan["findings"]) - 3
        if remaining > 0:
            _stderr(f"    ... {remaining} more finding(s)\n")
    elif initial_scan["status"] == "scan unavailable":
        _stderr(f"  Initial scan: unavailable ({len(initial_scan['errors'])} module error(s))\n")
        for error in initial_scan["errors"][:2]:
            _stderr(f"    {error['module']}: {error['error']}\n")
    else:
        summary = (
            "  Initial scan:"
            f" no findings yet ({initial_scan['functions_checked']} function(s) checked"
        )
        if initial_scan["skipped_functions"]:
            summary += f", {initial_scan['skipped_functions']} skipped"
        summary += ")\n"
        _stderr(summary)

    _stderr("\n  Files:\n")
    for r in generated:
        _stderr(f"    {r['path']}\n")
    for item in gap_stub_files:
        _stderr(f"    {item['path']}\n")
    if Path("ordeal.toml").exists():
        _stderr("    ordeal.toml\n")
    if ci_path:
        _stderr(f"    {ci_path}\n")
    if skill_path:
        _stderr(f"    {skill_path}\n")
    _stderr("\n")

    # --- Pinned values for review ---
    if pinned_values:
        _stderr("  Pinned values (verify these match intended behavior):\n")
        for expr in pinned_values:
            _stderr(f"    {expr}\n")
        _stderr("\n")

    # --- JSON to stdout for AI assistants ---
    import json

    report = {
        "package": pkg,
        "modules_scanned": len(results),
        "tests_generated": n_tests,
        "test_breakdown": {"pinned": n_pinned, "property": n_properties, "chaos": n_chaos},
        "properties_discovered": unique_props,
        "tests_pass": tests_pass,
        "mutation_score": mutation_score.removeprefix("Score: ") if mutation_score else None,
        "initial_scan": initial_scan,
        "close_gaps": close_gaps,
        "gap_stub_files": gap_stub_files,
        "weakest_tests": weakest_tests,
        "ci_workflow": _display_path(Path(ci_path)) if ci_path else None,
        "install_skill": install_skill,
        "skill": _display_path(Path(skill_path)) if skill_path else None,
        "files": [_display_path(Path(r["path"])) for r in generated if r["path"]]
        + [item["path"] for item in gap_stub_files]
        + ([_display_path(Path("ordeal.toml"))] if Path("ordeal.toml").exists() else [])
        + ([_display_path(Path(ci_path))] if ci_path else [])
        + ([_display_path(Path(skill_path))] if skill_path else []),
        "pinned_values": pinned_values,
        "functions": [
            {
                "module": r["module"],
                "status": r["status"],
                "test_file": _display_path(Path(r["path"])) if r["path"] else None,
            }
            for r in results
        ],
    }
    print(json.dumps(report, indent=2))

    return 0
