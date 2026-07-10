from __future__ import annotations
# ruff: noqa
# -- Terminal report --------------------------------------------------------


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print Ordeal Property Results and Mutation Results at the end."""
    # -- Warn if chaos tests were skipped (easy to miss in CI) --
    skipped = config.__dict__.get("_ordeal_skipped_chaos", 0) if hasattr(config, "__dict__") else 0
    if skipped:
        terminalreporter.section("Ordeal Warning")
        terminalreporter.line(
            f"WARNING: {skipped} chaos test(s) SKIPPED — run with --chaos to include them",
        )
        terminalreporter.line(
            "  Without --chaos, buggify() is inactive and chaos-marked tests don't run.",
        )
        terminalreporter.line("")

    # -- Seed corpus replay results --
    if _seed_replay_results:
        terminalreporter.section("Ordeal Seed Corpus")
        reproduced = sum(1 for s in _seed_replay_results if s["reproduced"])
        fixed = len(_seed_replay_results) - reproduced
        terminalreporter.line(
            f"  {len(_seed_replay_results)} seed(s) replayed: "
            f"{fixed} fixed, {reproduced} reproduced",
            green=(reproduced == 0),
            red=(reproduced > 0),
        )
        for sr in _seed_replay_results:
            if sr["reproduced"]:
                terminalreporter.line(
                    f"  REGRESSION  {sr['seed_name']}: {sr['test_class']} — {sr['error']}",
                    red=True,
                )
        _seed_replay_results.clear()

    # -- Mutation results (from @pytest.mark.mutate tests) --
    if _mutation_results:
        terminalreporter.section("Ordeal Mutation Results")
        for target, result in _mutation_results:
            terminalreporter.line(result.summary())
            terminalreporter.line("")
        _mutation_results.clear()

    # Show property and reliability reports when the tracker captured evidence.
    results = assertions.tracker.results
    reliability = assertions.tracker.reliability_results
    if not results and not reliability:
        return

    if results:
        terminalreporter.section("Ordeal Property Results")

        passed = [p for p in results if p.passed]
        failed = [p for p in results if not p.passed]

        for p in passed:
            terminalreporter.line(f"  PASS  {p.summary}", green=True)
        for p in failed:
            terminalreporter.line(f"  FAIL  {p.summary}", red=True)

        total = len(results)
        if failed:
            terminalreporter.line(f"\n  {len(failed)}/{total} properties FAILED", red=True)
        else:
            terminalreporter.line(f"\n  {total}/{total} properties passed", green=True)

        # Structured metadata — no hardcoded advice, just facts.
        seed = config.getoption("chaos_seed", default=None)
        kinds = {p.type for p in results if isinstance(getattr(p, "type", None), str)}
        terminalreporter.line("")
        terminalreporter.line(
            f"  Config: seed={'none' if seed is None else seed}, "
            f"assertion types used: {', '.join(sorted(kinds)) or 'none'}",
        )

    if reliability:
        terminalreporter.section("Ordeal Reliability Coverage")
        terminalreporter.line("  operation × fault × property")
        labels = [f"{cell.operation} × {cell.fault} × {cell.property}" for cell in reliability]
        width = max(len(label) for label in labels)
        for cell, label in zip(reliability, labels, strict=True):
            markup = {
                "PASS": {"green": True},
                "FAIL": {"red": True},
                "NOT EXERCISED": {"yellow": True},
            }[cell.status]
            terminalreporter.line(f"  {label:<{width}}  {cell.status}", **markup)

        counts = {
            status: sum(cell.status == status for cell in reliability)
            for status in ("PASS", "NOT EXERCISED", "FAIL")
        }
        terminalreporter.line("")
        terminalreporter.line(
            f"  {counts['PASS']} PASS, {counts['NOT EXERCISED']} NOT EXERCISED, "
            f"{counts['FAIL']} FAIL"
        )
