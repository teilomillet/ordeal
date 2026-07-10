from __future__ import annotations
# ruff: noqa
def _arg(*tokens: str, **kwargs: Any) -> ArgumentSpec:
    """Create a declarative CLI argument spec."""
    return ArgumentSpec(tokens=tokens, kwargs=dict(kwargs))
def _load_optional_config(path_str: str | None) -> OrdealConfig | None:
    """Load a config file when explicitly requested or present in cwd."""
    config_path = Path(path_str or "ordeal.toml")
    if not config_path.exists():
        if path_str is None:
            return None
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return load_config(config_path)
def _cli_or_config(value: Any, fallback: Any) -> Any:
    """Prefer an explicit CLI value, otherwise use the config/default fallback."""
    return fallback if value is None else value
def _make_signal_profiler(
    checkpoints: tuple[float, ...] = _BENCHMARK_SIGNAL_CHECKPOINTS,
) -> tuple[
    Callable[[ProgressSnapshot], None],
    Callable[[ExplorationResult], list[dict[str, int | float]]],
]:
    """Collect coarse anytime metrics at fixed wall-clock checkpoints."""
    ordered = [cp for cp in checkpoints if cp > 0]
    remaining = list(sorted(dict.fromkeys(ordered)))
    samples: list[dict[str, int | float]] = []

    def _capture(
        seconds: float,
        *,
        elapsed: float,
        runs: int,
        steps: int,
        edges: int,
        checkpoints_seen: int,
        failures: int,
    ) -> None:
        samples.append(
            {
                "seconds": seconds,
                "elapsed": elapsed,
                "runs": runs,
                "steps": steps,
                "edges": edges,
                "checkpoints": checkpoints_seen,
                "failures": failures,
            }
        )

    def _progress(snap: ProgressSnapshot) -> None:
        while remaining and snap.elapsed >= remaining[0]:
            seconds = remaining.pop(0)
            _capture(
                seconds,
                elapsed=snap.elapsed,
                runs=snap.total_runs,
                steps=snap.total_steps,
                edges=snap.unique_edges,
                checkpoints_seen=snap.checkpoints,
                failures=snap.failures,
            )

    def _finalize(result: ExplorationResult) -> list[dict[str, int | float]]:
        for seconds in remaining:
            _capture(
                seconds,
                elapsed=result.duration_seconds,
                runs=result.total_runs,
                steps=result.total_steps,
                edges=result.unique_edges,
                checkpoints_seen=result.checkpoints_saved,
                failures=len(result.failures),
            )
        return samples

    return _progress, _finalize
# ============================================================================
# Commands
# ============================================================================


def _cmd_catalog(args: argparse.Namespace) -> int:
    """Print all ordeal capabilities, organized by subsystem."""
    from ordeal import catalog

    c = catalog()
    if getattr(args, "json", False):
        print(json.dumps(c, indent=2, sort_keys=True))
        return 0

    total = sum(len(v) for v in c.values())
    print(f"{total} capabilities across {len(c)} subsystems:\n")
    for key in sorted(c):
        entries = c[key]
        first_doc = str(
            (entries[0].get("subsystem_summary") if entries else "")
            or (entries[0].get("capability") if entries else "")
            or (entries[0].get("doc") if entries else "")
        ).strip()
        names = ", ".join(e["name"] for e in entries[:4])
        if len(entries) > 4:
            names += ", ..."
        print(f"  {key} ({len(entries)}) — {first_doc}")
        print(f"    {names}")

    command_entries = c.get("cli", [])
    if command_entries:
        print("\nCLI commands:")
        for entry in command_entries:
            outputs = ", ".join(str(item) for item in entry.get("outputs", [])[:2])
            suffix = f" | outputs: {outputs}" if outputs else ""
            print(f"  {entry['name']:<10} {entry.get('capability', entry.get('doc', ''))}{suffix}")
    print("\nRun 'ordeal --help' for the focused beginner workflow.")
    print("Run 'ordeal <command> --help' for command-specific options.")
    print("Run 'ordeal catalog --detail' for applicability, inputs, outputs, and examples.")
    print("Run 'ordeal catalog --json' for the machine-readable capability map.")
    print("Run 'ordeal skill' or 'ordeal init --install-skill' for local agent guidance.")
    print("Python: from ordeal import catalog; catalog()")

    if getattr(args, "detail", False):
        for key in sorted(c):
            entries = c[key]
            print(f"\n{key} ({len(entries)}):")
            for item in entries:
                doc = item["doc"]
                sig = item.get("signature", "")
                print(f"  {item['name']}{sig}")
                if doc:
                    print(f"    {doc}")
                capability = str(item.get("capability", "")).strip()
                applies_to = str(item.get("applies_to", "")).strip()
                if capability and capability != doc:
                    print(f"    capability: {capability}")
                if applies_to:
                    print(f"    applies_to: {applies_to}")
                inputs = [str(value) for value in item.get("inputs", []) if str(value).strip()]
                if inputs:
                    print(f"    inputs: {', '.join(inputs)}")
                outputs = [str(value) for value in item.get("outputs", []) if str(value).strip()]
                if outputs:
                    print(f"    outputs: {', '.join(outputs)}")
                call_pattern = str(item.get("call_pattern", "")).strip()
                if call_pattern and key != "cli":
                    print(f"    call_pattern:\n{indent(call_pattern, '      ')}")
                examples = [
                    str(value).rstrip() for value in item.get("examples", []) if str(value).strip()
                ]
                if examples:
                    print("    examples:")
                    for example in examples[:3]:
                        print(indent(example, "      "))
                learn_more = [
                    str(value) for value in item.get("learn_more", []) if str(value).strip()
                ]
                if learn_more:
                    print(f"    learn_more: {', '.join(learn_more)}")

    return 0
