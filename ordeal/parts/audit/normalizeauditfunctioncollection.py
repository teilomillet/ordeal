from __future__ import annotations


# ruff: noqa
def _normalize_audit_function_collection(
    collected: tuple[Any, ...],
) -> tuple[list[tuple[str, object]], list[str], list[tuple[str, object]]]:
    """Normalize collector output across tuple-shape variations.

    The current implementation returns ``(scannable, skipped, discovered)``,
    but older call sites and cached environments may still provide the older
    ``(scannable, skipped)`` shape. Preserve epistemic reporting by filling in
    placeholder discovered entries when necessary.
    """
    if len(collected) == 3:
        scannable, skipped, discovered = collected
        return list(scannable), list(skipped), list(discovered)
    if len(collected) == 2:
        scannable, skipped = collected
        discovered = list(scannable) + [(name, object()) for name in skipped]
        return list(scannable), list(skipped), discovered
    raise ValueError(f"unexpected audit collection shape: {len(collected)}")


def _audit_contract_findings(
    functions: Sequence[tuple[str, object]],
    *,
    contract_checks: Mapping[str, Sequence[Any]] | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Run explicit contract probes against discovered audit callables."""
    findings: list[dict[str, Any]] = []
    if not contract_checks:
        return findings
    for name, func in functions:
        checks = list(contract_checks.get(name, []))
        if not checks:
            continue
        try:
            _violations, details = _evaluate_contract_checks(func, checks)
        except Exception as exc:
            warnings.append(f"contract checks failed for {name}: {type(exc).__name__}: {exc}")
            continue
        for detail in details:
            enriched = dict(detail)
            enriched.setdefault("function", name)
            findings.append(enriched)
    return findings


def _audit_blocking_reason(
    *,
    total_functions: int,
    gap_functions: Sequence[str],
    discovered_functions: Sequence[tuple[str, object]],
    min_fixture_completeness: float,
) -> str | None:
    """Return an early blocking reason when audit lacks enough runnable leverage."""
    if total_functions <= 0:
        return "no callable targets were discovered"
    completeness = max(total_functions - len(gap_functions), 0) / max(total_functions, 1)
    if completeness <= 0.0:
        if any("." in name for name, _func in discovered_functions):
            return "need instance/state harness or object/state factory for discovered methods"
        return "no discovered targets had inferable fixtures or strategies"
    if min_fixture_completeness > 0.0 and completeness < min_fixture_completeness:
        return (
            "fixture completeness is too low for meaningful audit "
            f"({completeness:.0%} < {min_fixture_completeness:.0%})"
        )
    return None


def _audit_harness_hints(
    discovered_functions: Sequence[tuple[str, object]],
    gap_functions: Sequence[str],
) -> list[dict[str, Any]]:
    """Mine concrete harness suggestions for blocked instance-method targets."""
    hints: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    missing = set(gap_functions)
    for name, func in discovered_functions:
        if missing and name not in missing:
            continue
        owner = getattr(func, "__ordeal_owner__", None)
        if owner is None:
            continue
        for hint in _mine_object_harness_hints(
            getattr(owner, "__module__", ""),
            getattr(owner, "__name__", "Owner"),
            name.rsplit(".", 1)[-1],
        )[:5]:
            key = (name, hint.kind, hint.suggestion)
            if key in seen:
                continue
            seen.add(key)
            hints.append(
                {
                    "function": name,
                    "kind": hint.kind,
                    "suggestion": hint.suggestion,
                    "evidence": hint.evidence,
                    "confidence": round(float(hint.confidence), 2),
                    "score": round(float(hint.score), 3),
                    "signals": list(hint.signals),
                    "config": dict(hint.config),
                }
            )
    return hints


def _function_name_in_nodeid(function_name: str, nodeid: str) -> bool:
    """Return True when *nodeid* directly mentions *function_name*."""
    tail = nodeid.rsplit("::", 1)[-1]
    return function_name in tail or function_name in nodeid


def _should_collect_pytest_nodeids(
    functions: list[tuple[str, object]],
    *,
    current_coverage: CoverageMeasurement,
    test_file_evidence: list[TestFileEvidence],
) -> bool:
    """Return True when pytest node IDs add signal beyond verified coverage."""
    if not functions or not test_file_evidence:
        return False
    if current_coverage.status != Status.VERIFIED:
        return True
    for _name, func in functions:
        body_lines = _function_body_line_numbers(func)
        if not body_lines:
            return True
        if body_lines.issubset(current_coverage.missing_lines):
            return True
    return False


def _build_function_audits(
    functions: list[tuple[str, object]],
    *,
    current_coverage: CoverageMeasurement,
    test_file_evidence: list[TestFileEvidence],
    collected_nodeids: dict[Path, list[str]],
) -> list[FunctionAudit]:
    """Build an epistemic function-level coverage map."""
    audits: list[FunctionAudit] = []

    for name, func in functions:
        body_lines = _function_body_line_numbers(func)
        total_body_lines = len(body_lines) if body_lines is not None else 0
        covered_body_lines = 0
        evidence: list[dict[str, str]] = []

        if body_lines and current_coverage.status == Status.VERIFIED:
            covered = sorted(body_lines - current_coverage.missing_lines)
            covered_body_lines = len(covered)
            if covered_body_lines > 0:
                evidence.append(
                    {
                        "kind": "coverage_lines",
                        "epistemic": "verified",
                        "detail": (
                            f"coverage hits {covered_body_lines}/{total_body_lines} body line(s)"
                        ),
                    }
                )

        direct_nodeids = [
            nodeid
            for nodeids in collected_nodeids.values()
            for nodeid in nodeids
            if _function_name_in_nodeid(name, nodeid)
        ]
        if direct_nodeids:
            evidence.append(
                {
                    "kind": "pytest_nodeid",
                    "epistemic": "inferred",
                    "detail": ", ".join(direct_nodeids[:DISPLAY_CAP]),
                }
            )

        if covered_body_lines > 0:
            status: FunctionAuditStatus = "exercised"
            epistemic: EvidenceLabel = "verified"
        elif test_file_evidence:
            status = "exploratory"
            epistemic = "inferred"
            if not evidence:
                evidence.extend(
                    {
                        "kind": item.basis,
                        "epistemic": item.epistemic,
                        "detail": item.path,
                    }
                    for item in test_file_evidence[:DISPLAY_CAP]
                )
        else:
            status = "uncovered"
            epistemic = "none"
            evidence.append(
                {
                    "kind": "no_tests",
                    "epistemic": "none",
                    "detail": "no matching pytest files or collected nodeids",
                }
            )

        audits.append(
            FunctionAudit(
                name=name,
                status=status,
                epistemic=epistemic,
                covered_body_lines=covered_body_lines,
                total_body_lines=total_body_lines,
                evidence=evidence,
            )
        )

    return audits


def _mine_audit_functions(
    functions: list[tuple[str, object]],
    *,
    max_examples: int,
    warnings: list[str],
) -> dict[str, MineResult]:
    """Mine each scannable function once for reuse across the audit.

    The resulting mine outputs drive both generated property tests and the
    human-readable summary, avoiding redundant calls to ``mine()``.
    """
    results: dict[str, MineResult] = {}
    for name, func in functions:
        try:
            results[name] = mine(func, max_examples=max_examples)
        except Exception as exc:
            warnings.append(f"mining failed for {name}: {type(exc).__name__}: {exc}")
    return results


def _is_generated_test_file(path: Path) -> bool:
    """Return True when *path* lives under the generated ``.ordeal`` tree."""
    return ".ordeal" in path.parts


# ============================================================================
# Coverage measurement — via JSON, not stdout parsing
# ============================================================================


def _coverage_runtime_context(
    test_files: list[Path],
    module_name: str | None = None,
) -> tuple[str, dict[str, str], list[str], bool]:
    """Build subprocess coverage context for a set of test files."""
    cwd = str(Path.cwd())
    generated_only = all(_is_generated_test_file(f) for f in test_files)
    in_project = any(str(f).startswith(cwd) and "/.ordeal/" not in str(f) for f in test_files)
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = cwd
    pytest_args = [
        *[str(nodeid) for nodeid in _exact_pytest_nodeids(test_files, module_name=module_name)],
        "-q",
        "--tb=no",
        "--no-header",
        "-o",
        "addopts=",
        "-p",
        "no:ordeal",
    ]

    if not in_project:
        pytest_args.extend(["--override-ini", f"confcutdir={test_files[0].parent}"])

    return cwd, env, pytest_args, generated_only


def _is_import_facade(module_name: str | None) -> bool:
    """Return whether *module_name* is a generated import-only facade."""
    if not module_name:
        return False
    try:
        spec = importlib.util.find_spec(module_name)
        source_file = Path(str(getattr(spec, "origin", "")))
        source = source_file.read_text(encoding="utf-8")
    except (ImportError, OSError, TypeError, UnicodeDecodeError, ValueError):
        return False
    return (
        "_PART_FILES" in source and "_load_facade_parts()" in source and "exec(compile(" in source
    )


def _test_file_imports_module(tree: ast.Module, module_name: str) -> bool:
    """Return whether the test module imports the target at module scope."""
    for node in tree.body:
        if isinstance(node, ast.Import) and any(alias.name == module_name for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            return True
    return False


def _exact_pytest_nodeids(
    test_files: Sequence[Path],
    *,
    module_name: str | None = None,
) -> list[str]:
    """Return static pytest node IDs, falling back to file paths when uncertain.

    Conventional test modules can be selected without a separate pytest
    collection process. Dynamic collection hooks retain file-level selection so
    audit never guesses away executable coverage evidence.
    """
    selected: list[str] = []
    for test_file in test_files:
        path_text = str(test_file)
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=path_text)
        except (OSError, SyntaxError, UnicodeDecodeError):
            selected.append(path_text)
            continue
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("pytest_")
            for node in tree.body
        ):
            selected.append(path_text)
            continue

        nodeids: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                nodeids.append(f"{path_text}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                methods = [
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name.startswith("test_")
                ]
                if not methods:
                    selected.append(path_text)
                    break
                nodeids.extend(f"{path_text}::{node.name}::{method}" for method in methods)
        else:
            if (
                nodeids
                and _is_import_facade(module_name)
                and module_name is not None
                and _test_file_imports_module(tree, module_name)
            ):
                selected.append(nodeids[0])
            else:
                selected.extend(nodeids or [path_text])
    return selected


def _measure_coverage(
    test_files: list[Path],
    module_name: str,
) -> CoverageMeasurement:
    """Run tests and measure coverage via coverage.py or an internal tracer.

    **Preferred path:** When ``coverage.py`` is available, ordeal runs
    pytest under its tracer and reads a structured JSON report. The JSON
    schema is stable and easy to cross-check.

    **Fallback path:** When ``coverage.py`` is not installed, ordeal traces
    the target module directly in a subprocess and computes executed/missing
    lines itself. This keeps ``ordeal audit`` usable in a fresh environment.
    """
    if not test_files:
        return CoverageMeasurement(Status.FAILED, error="no test files provided")

    cwd, env, pytest_args, generated_only = _coverage_runtime_context(test_files, module_name)

    if generated_only:
        if importlib.util.find_spec("coverage") is not None:
            return _measure_generated_coverage_with_coverage_py(module_name, test_files, cwd, env)
        return _measure_generated_coverage_with_trace(module_name, test_files, cwd, env)

    if importlib.util.find_spec("coverage") is not None:
        return _measure_coverage_with_coverage_py(module_name, pytest_args, cwd, env)

    if importlib.util.find_spec("pytest_cov") is not None:
        return _measure_coverage_with_pytest_cov(module_name, pytest_args, cwd, env)

    return _measure_coverage_with_trace(module_name, pytest_args, cwd, env)
