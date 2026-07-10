from __future__ import annotations


# ruff: noqa
def _cmd_mutate(args: argparse.Namespace) -> int:
    """Run mutation testing on specified targets."""
    from ordeal.mutations import (
        MutationResult,
        NoTestsFoundError,
        generate_starter_tests,
        mutate,
    )

    targets: list[str] = args.targets or []
    preset: str | None = args.preset
    operators: list[str] | None = None
    workers: int = args.workers
    threshold: float = args.threshold
    filter_equivalent: bool = not args.no_filter
    equivalence_samples: int = args.equivalence_samples
    test_filter: str | None = args.test_filter
    mutant_timeout: float | None = args.mutant_timeout
    disk_mutation: bool | None = args.disk_mutation
    resume: bool = args.resume
    promote_clusters_only = True
    cluster_min_size = 2
    cfg: OrdealConfig | None = None

    try:
        cfg = _load_optional_config(args.config)
    except FileNotFoundError:
        if args.config is not None:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=str(args.config),
                        summary=f"config file not found: {args.config}",
                        blocking_reason=f"config file not found: {args.config}",
                        raw_details={"config": args.config},
                    ).to_json()
                )
            else:
                _stderr(f"Config file not found: {args.config}\n")
            return 1
    except ConfigError as e:
        if getattr(args, "json", False):
            print(
                _build_blocked_agent_envelope(
                    tool="mutate",
                    target=str(args.config or "ordeal.toml"),
                    summary=f"config error in {args.config or 'ordeal.toml'}",
                    blocking_reason=str(e),
                    raw_details={"config": args.config or "ordeal.toml"},
                ).to_json()
            )
        else:
            _stderr(f"Config error: {e}\n")
        return 1

    # Fall back to config file if no targets given
    if not targets:
        config_path = args.config or "ordeal.toml"
        if cfg is None:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary="no mutation targets configured",
                        blocking_reason="no targets specified and no config file found",
                        suggested_commands=(
                            "ordeal mutate myapp.scoring.compute",
                            "ordeal mutate myapp.scoring",
                        ),
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
            _stderr(
                "No targets specified. Use positional args or [mutations] in ordeal.toml.\n"
                "  ordeal mutate myapp.scoring.compute\n"
                "  ordeal mutate myapp.scoring\n"
            )
            return 1

        if cfg.mutations is None:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary="no [mutations] section in config",
                        blocking_reason="config has no [mutations] section",
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
            _stderr("No [mutations] section in config.\n")
            return 1

        targets = cfg.mutations.targets
        if not targets:
            if getattr(args, "json", False):
                print(
                    _build_blocked_agent_envelope(
                        tool="mutate",
                        target=config_path,
                        summary="no mutation targets in config",
                        blocking_reason="config [mutations] section has no targets",
                        raw_details={"config": config_path},
                    ).to_json()
                )
                return 1
            _stderr("No targets in [mutations] section.\n")
            return 1

    if cfg is not None and cfg.mutations is not None:
        # Config provides defaults; explicit CLI flags override.
        if preset is None and cfg.mutations.operators is None:
            preset = cfg.mutations.preset
        if cfg.mutations.operators is not None and preset is None:
            operators = cfg.mutations.operators
        if args.workers == 0 and cfg.mutations.workers > 0:
            workers = cfg.mutations.workers
        if args.threshold == 0.0 and cfg.mutations.threshold > 0.0:
            threshold = cfg.mutations.threshold
        if not args.no_filter:
            filter_equivalent = cfg.mutations.filter_equivalent
        if args.equivalence_samples == 10 and cfg.mutations.equivalence_samples != 10:
            equivalence_samples = cfg.mutations.equivalence_samples
        if test_filter is None and cfg.mutations.test_filter is not None:
            test_filter = cfg.mutations.test_filter
        if mutant_timeout is None and cfg.mutations.mutant_timeout is not None:
            mutant_timeout = cfg.mutations.mutant_timeout
        promote_clusters_only = cfg.mutations.promote_clusters_only
        cluster_min_size = cfg.mutations.cluster_min_size

    # Default preset when nothing specified
    if preset is None and operators is None:
        preset = "standard"

    all_results: list[tuple[str, MutationResult]] = []
    blockers: list[dict[str, Any]] = []
    exit_code = 0
    stubs_path = Path(args.generate_stubs) if args.generate_stubs else None

    for target in targets:
        if not getattr(args, "json", False):
            _stderr(f"Mutating {target}...\n")
        contract_context = _mutation_contract_context_for_target(cfg, target)

        try:
            if getattr(args, "json", False):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = mutate(
                        target,
                        operators=operators,
                        preset=preset,
                        workers=workers,
                        filter_equivalent=filter_equivalent,
                        equivalence_samples=equivalence_samples,
                        test_filter=test_filter,
                        mutant_timeout=mutant_timeout,
                        disk_mutation=disk_mutation,
                        promote_clusters_only=promote_clusters_only,
                        cluster_min_size=cluster_min_size,
                        resume=resume,
                        contract_context=contract_context,
                    )
            else:
                result = mutate(
                    target,
                    operators=operators,
                    preset=preset,
                    workers=workers,
                    filter_equivalent=filter_equivalent,
                    equivalence_samples=equivalence_samples,
                    test_filter=test_filter,
                    mutant_timeout=mutant_timeout,
                    disk_mutation=disk_mutation,
                    promote_clusters_only=promote_clusters_only,
                    cluster_min_size=cluster_min_size,
                    resume=resume,
                    contract_context=contract_context,
                )
        except NoTestsFoundError as e:
            if not getattr(args, "json", False):
                _stderr(f"  WARNING: No tests found for {target!r}\n")
            starter = generate_starter_tests(target)
            suggested = e.suggested_file or f"tests/test_{target.rsplit('.', 1)[-1]}.py"
            blockers.append(
                {
                    "target": target,
                    "summary": f"No tests found for {target}",
                    "suggested_test_file": suggested,
                    "starter_tests": starter,
                }
            )
            if starter:
                if args.generate_stubs:
                    assert stubs_path is not None
                    stubs_path.parent.mkdir(parents=True, exist_ok=True)
                    existing = (
                        stubs_path.read_text(encoding="utf-8") if stubs_path.exists() else ""
                    )
                    sep = "\n\n" if existing else ""
                    stubs_path.write_text(existing + sep + starter, encoding="utf-8")
                    if not getattr(args, "json", False):
                        _stderr(f"  Starter tests written: {stubs_path}\n")
                elif not getattr(args, "json", False):
                    # Print the scaffold directly — don't hide it behind a flag
                    print(f"\n# Save to: {suggested}\n")
                    print(starter)
                    _stderr(f"  Or run: ordeal init {target}\n")
            exit_code = 1
            continue
        except (ImportError, AttributeError, ValueError) as e:
            if getattr(args, "json", False):
                blockers.append(
                    {
                        "target": target,
                        "summary": str(e),
                        "suggested_test_file": None,
                        "starter_tests": None,
                    }
                )
            else:
                _stderr(f"  Error: {e}\n")
            exit_code = 1
            continue

        all_results.append((target, result))
        if not getattr(args, "json", False):
            print(result.summary())
            print()

        if threshold > 0.0 and result.score < threshold:
            exit_code = 1

    # Generate test stubs if requested
    if args.generate_stubs and stubs_path is not None:
        all_stubs: list[str] = []
        for _, result in all_results:
            stub = result.generate_test_stubs()
            if stub:
                all_stubs.append(stub)
        if all_stubs:
            stubs_path.parent.mkdir(parents=True, exist_ok=True)
            stubs_path.write_text("\n\n".join(all_stubs), encoding="utf-8")
            if not getattr(args, "json", False):
                _stderr(f"Test stubs written: {stubs_path}\n")

    # Final score line — always printed for CI parseability
    if all_results:
        total_mutants = sum(r.total for _, r in all_results)
        total_killed = sum(r.killed for _, r in all_results)
        overall = total_killed / total_mutants if total_mutants > 0 else 1.0
        if len(all_results) > 1 and not getattr(args, "json", False):
            print(f"Overall: {total_killed}/{total_mutants} ({overall:.0%})")
        if not getattr(args, "json", False):
            print(f"Score: {total_killed}/{total_mutants} ({overall:.0%})")
        if threshold > 0.0 and not getattr(args, "json", False):
            status = "PASS" if overall >= threshold else "FAIL"
            print(f"Threshold: {threshold:.0%} — {status}")

    surface_groups: list[dict[str, Any]] = []
    if targets:
        with contextlib.suppress(Exception):
            surface_groups = _canonical_surface_groups_for_targets(
                targets,
                cfg=cfg,
                object_specs=list(cfg.objects) if cfg is not None else [],
            )

    if getattr(args, "json", False):
        print(
            _build_mutate_agent_envelope(
                targets=targets,
                results=all_results,
                blockers=blockers,
                threshold=threshold,
                stubs_path=stubs_path,
                surface_groups=surface_groups,
            ).to_json()
        )
        if any(result.survived for _, result in all_results):
            exit_code = 1

    return exit_code


# ============================================================================
# Reporting
# ============================================================================


def _telemetry_root(result: Any) -> Any | None:
    """Return the nested telemetry container if one exists."""
    return _result_value(result, "telemetry", "telemetry_info", "exploration_telemetry")


def _result_value(source: Any, *names: str) -> Any | None:
    """Return the first non-None value found under one of *names*."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
        return None
    for name in names:
        if hasattr(source, name):
            value = getattr(source, name)
            if value is not None:
                return value
    return None


def _lookup_telemetry_value(result: Any, *names: str) -> Any | None:
    """Return telemetry data from the nested telemetry object or the result itself."""
    root = _telemetry_root(result)
    value = _result_value(root, *names)
    if value is not None:
        return value
    return _result_value(result, *names)


def _count_entries(value: Any) -> int | None:
    """Return a size-like count for mappings or sequences."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        if value and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()
        ):
            return sum(int(v) for v in value.values())
        return len(value)
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return len(value)
    except TypeError:
        return None


def _normalize_telemetry_label(value: Any) -> str | None:
    """Return a compact human-readable label for one telemetry item."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("_", " ")


def _telemetry_item_label(item: Any) -> str | None:
    """Return a best-effort label for one telemetry item."""
    for name in ("kind", "category", "type", "status", "error_type"):
        value = _result_value(item, name)
        label = _normalize_telemetry_label(value)
        if label:
            return label

    exit_code = _result_value(item, "exit_code", "returncode")
    signal_value = _result_value(item, "signal", "terminated_by_signal")
    if isinstance(exit_code, int):
        if exit_code < 0 or signal_value is not None:
            return "signal death"
        if exit_code != 0:
            return "nonzero exit"
    if signal_value is not None:
        return "signal death"
    return None


def _summarize_telemetry_items(value: Any) -> tuple[int | None, Counter[str]]:
    """Return a count and label histogram for a telemetry collection."""
    if value is None:
        return None, Counter()
    if isinstance(value, Mapping):
        if value and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()
        ):
            histogram = Counter()
            total = 0
            for key, count in value.items():
                label = _normalize_telemetry_label(key)
                if label is None:
                    continue
                histogram[label] += int(count)
                total += int(count)
            return total, histogram
        items: list[Any] = list(value.values())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
    else:
        items = [value]

    histogram: Counter[str] = Counter()
    for item in items:
        label = _telemetry_item_label(item)
        if label is not None:
            histogram[label] += 1
    return len(items), histogram


def _format_counter(counter: Counter[str]) -> str | None:
    """Render a compact count histogram."""
    if not counter:
        return None
    parts = [f"{label} x{count}" for label, count in counter.most_common(3)]
    return ", ".join(parts) if parts else None


def _format_mapping_counts(
    value: Any,
    *,
    preferred_keys: Sequence[str] = (),
    max_items: int = 3,
) -> str | None:
    """Render a compact summary for one telemetry mapping."""
    if not isinstance(value, Mapping):
        return None
    for key in ("summary", "description", "text"):
        summary = value.get(key)
        if isinstance(summary, str) and summary.strip():
            return summary.strip()

    items: list[str] = []
    for key in preferred_keys:
        entry = value.get(key)
        if entry is None:
            continue
        if isinstance(entry, bool):
            if entry:
                items.append(str(key))
        elif isinstance(entry, (int, float, str)):
            items.append(f"{key}={entry}")
        elif isinstance(entry, Mapping):
            count = _count_entries(entry)
            if count is not None:
                items.append(f"{key}={count}")
        elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, bytearray)):
            items.append(f"{key}={len(entry)}")
        if len(items) >= max_items:
            return ", ".join(items)

    if not items:
        for key, entry in value.items():
            if key in {"summary", "description", "text"}:
                continue
            if isinstance(entry, bool):
                if entry:
                    items.append(str(key))
            elif isinstance(entry, (int, float, str)):
                items.append(f"{key}={entry}")
            elif isinstance(entry, Mapping):
                count = _count_entries(entry)
                if count is not None:
                    items.append(f"{key}={count}")
            elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, bytearray)):
                items.append(f"{key}={len(entry)}")
            if len(items) >= max_items:
                break

    return ", ".join(items) if items else None


def _resolve_telemetry_section(result: Any, *names: str) -> Any | None:
    """Return the first available telemetry section by name."""
    for source in (_telemetry_root(result), result):
        value = _result_value(source, *names)
        if value is not None:
            return value
    return None
