from __future__ import annotations
# ruff: noqa
def _module_audit_to_dict(result: ModuleAudit) -> dict[str, object]:
    """Serialize a module audit result for the on-disk cache."""
    return {
        "module": result.module,
        "current_test_count": result.current_test_count,
        "current_test_lines": result.current_test_lines,
        "current_coverage": _coverage_measurement_to_dict(result.current_coverage),
        "migrated_test_count": result.migrated_test_count,
        "migrated_lines": result.migrated_lines,
        "migrated_coverage": _coverage_measurement_to_dict(result.migrated_coverage),
        "mined_properties": result.mined_properties,
        "mutation_score": result.mutation_score,
        "validation_mode": result.validation_mode,
        "gap_functions": result.gap_functions,
        "total_functions": result.total_functions,
        "function_audits": [_function_audit_to_dict(item) for item in result.function_audits],
        "suggestions": result.suggestions,
        "suggested_relations": result.suggested_relations,
        "mutation_gaps": result.mutation_gaps,
        "weakest_tests": result.weakest_tests,
        "mutation_gap_stubs": result.mutation_gap_stubs,
        "mutation_targets": result.mutation_targets,
        "contract_findings": result.contract_findings,
        "blocking_reason": result.blocking_reason,
        "harness_hints": result.harness_hints,
        "surface": result.surface,
        "not_checked": result.not_checked,
        "warnings": result.warnings,
        "generated_test": result.generated_test,
        "evidence_views": result.evidence_views(),
    }
def _module_audit_from_dict(data: dict[str, object]) -> ModuleAudit:
    """Deserialize a cached module audit result."""
    return ModuleAudit(
        module=str(data["module"]),
        current_test_count=int(data["current_test_count"]),
        current_test_lines=int(data["current_test_lines"]),
        current_coverage=_coverage_measurement_from_dict(data["current_coverage"]),
        migrated_test_count=int(data["migrated_test_count"]),
        migrated_lines=int(data["migrated_lines"]),
        migrated_coverage=_coverage_measurement_from_dict(data["migrated_coverage"]),
        mined_properties=list(data.get("mined_properties", [])),
        mutation_score=str(data.get("mutation_score", "")),
        validation_mode=_normalize_validation_mode(str(data.get("validation_mode", "fast"))),
        gap_functions=list(data.get("gap_functions", [])),
        total_functions=int(data.get("total_functions", 0)),
        function_audits=[
            _function_audit_from_dict(item) for item in data.get("function_audits", [])
        ],
        suggestions=list(data.get("suggestions", [])),
        suggested_relations=list(data.get("suggested_relations", [])),
        mutation_gaps=list(data.get("mutation_gaps", [])),
        weakest_tests=list(data.get("weakest_tests", [])),
        mutation_gap_stubs=list(data.get("mutation_gap_stubs", [])),
        mutation_targets=list(data.get("mutation_targets", [])),
        contract_findings=list(data.get("contract_findings", [])),
        blocking_reason=(
            str(data["blocking_reason"]) if data.get("blocking_reason") is not None else None
        ),
        harness_hints=list(data.get("harness_hints", [])),
        surface=list(data.get("surface", [])),
        not_checked=list(data.get("not_checked", [])),
        warnings=list(data.get("warnings", [])),
        generated_test=str(data.get("generated_test", "")),
    )
def _render_audit_results(results: Sequence[ModuleAudit]) -> str:
    """Render a human-readable audit report from precomputed results."""
    lines = ["ordeal audit"]
    total_cur_tests = 0
    total_cur_lines = 0
    total_mig_tests = 0
    total_mig_lines = 0
    total_warnings = 0

    for result in results:
        lines.append(result.summary())
        total_cur_tests += result.current_test_count
        total_cur_lines += result.current_test_lines
        total_mig_tests += result.migrated_test_count
        total_mig_lines += result.migrated_lines
        total_warnings += len(result.warnings)

    if len(results) > 1:
        lines.append("\n  total:")
        lines.append(f"    current suite: {total_cur_tests} tests | {total_cur_lines} lines")
        lines.append(
            f"    generated incremental: {total_mig_tests} tests | {total_mig_lines} lines"
        )
        if total_cur_tests > 0:
            label, summary = _format_change_summary(
                total_cur_tests,
                total_mig_tests,
                total_cur_lines,
                total_mig_lines,
            )
            lines.append(f"    {label}:   {summary}")
        if total_warnings > 0:
            lines.append(f"    warnings: {total_warnings} (run with --verbose)")

    return "\n".join(lines)
# ============================================================================
# File counting — with explicit failure handling
# ============================================================================


def _count_tests_in_file(path: Path) -> tuple[int, str | None]:
    """Count ``def test_`` occurrences in a file.

    Returns ``(count, error)``.  If the file can't be read, returns
    ``(0, "reason")`` — never silently returns 0.

    **Limitation:** Counts by string match, not AST parsing.
    May overcount ``def test_`` in docstrings/comments.
    May undercount parameterized test generators.
    """
    try:
        text = path.read_text(encoding="utf-8")
        return text.count("def test_"), None
    except OSError as exc:
        return 0, f"cannot read {path.name}: {exc}"
def _count_lines_in_file(path: Path) -> tuple[int, str | None]:
    """Count non-empty lines in a file.

    Returns ``(count, error)``.  Non-empty = at least one non-whitespace char.

    **Why non-empty:** Empty lines and comment-only lines inflate the
    count.  Non-empty lines better represent code volume.
    """
    try:
        text = path.read_text(encoding="utf-8")
        return sum(1 for line in text.splitlines() if line.strip()), None
    except OSError as exc:
        return 0, f"cannot read {path.name}: {exc}"
# ============================================================================
# Test file discovery
# ============================================================================


def _pytest_collected_test_files(test_dir: Path) -> list[Path]:
    """Ask pytest which files it would collect beneath *test_dir*."""
    if not test_dir.is_dir():
        return []

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:ordeal",
                str(test_dir),
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if completed.returncode not in (0, 1, 5):
        return []

    seen: set[Path] = set()
    results: list[Path] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("=") or "collected " in line:
            continue
        path_text = line.split("::", 1)[0]
        if not path_text.endswith(".py"):
            continue
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if candidate.exists() and candidate not in seen:
            seen.add(candidate)
            results.append(candidate)
    return results
def _looks_like_test_file(path: Path) -> bool:
    """Return whether *path* appears to define pytest-style tests."""
    if path.name in {"conftest.py", "__init__.py"}:
        return False

    stem = path.stem
    if stem.startswith("test_") or stem.endswith("_test"):
        return True

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    return "def test_" in text or "class Test" in text
def _find_test_files(module_name: str, test_dir: Path) -> list[Path]:
    """Find test files that primarily test the given module.

    First uses cheap filesystem/AST checks to find likely test modules.
    Filename conventions (``test_<short>.py``, ``test_<short>_*.py``,
    and ``<short>_test.py``) still win when present. When naming does
    not help, import matching over likely test files handles non-standard
    names without paying a subprocess cost. Pytest collection remains the
    last fallback for custom collection setups.
    """
    import ast

    results: list[Path] = []
    mod_short = module_name.rsplit(".", 1)[-1]

    if not test_dir.is_dir():
        return results

    python_files = sorted(path.resolve() for path in test_dir.rglob("*.py") if path.is_file())
    if not python_files:
        return []
    candidates = [path for path in python_files if _looks_like_test_file(path)]

    for test_file in candidates:
        stem = test_file.stem
        if (
            stem == f"test_{mod_short}"
            or stem.startswith(f"test_{mod_short}_")
            or stem == f"{mod_short}_test"
        ):
            results.append(test_file)

    if results:
        return results

    def _imports_target(path: Path) -> bool:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name:
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == module_name:
                    return True
                if node.module:
                    for alias in node.names:
                        if f"{node.module}.{alias.name}" == module_name:
                            return True
        return False

    results = [path for path in candidates if _imports_target(path)]
    if results:
        return results

    collected = _pytest_collected_test_files(test_dir)
    if not collected:
        return []

    for test_file in collected:
        stem = test_file.stem
        if (
            stem == f"test_{mod_short}"
            or stem.startswith(f"test_{mod_short}_")
            or stem == f"{mod_short}_test"
        ):
            results.append(test_file)

    if results:
        return results

    return [path for path in collected if _imports_target(path)]
def _find_test_file_evidence(module_name: str, test_dir: Path) -> list[TestFileEvidence]:
    """Return test-file evidence with an explicit epistemic basis."""
    import ast

    mod_short = module_name.rsplit(".", 1)[-1]

    if not test_dir.is_dir():
        return []

    python_files = sorted(path.resolve() for path in test_dir.rglob("*.py") if path.is_file())
    if not python_files:
        return []
    candidates = [path for path in python_files if _looks_like_test_file(path)]

    def _imports_target(path: Path) -> bool:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name:
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == module_name:
                    return True
                if node.module:
                    for alias in node.names:
                        if f"{node.module}.{alias.name}" == module_name:
                            return True
        return False

    def _wrap(
        paths: list[Path],
        basis: Literal["filename", "import", "pytest_collection"],
    ) -> list[TestFileEvidence]:
        return [
            TestFileEvidence(
                path=str(path),
                basis=basis,
                epistemic="verified" if basis == "pytest_collection" else "inferred",
            )
            for path in paths
        ]

    filename_matches = [
        path
        for path in candidates
        if (
            path.stem == f"test_{mod_short}"
            or path.stem.startswith(f"test_{mod_short}_")
            or path.stem == f"{mod_short}_test"
        )
    ]
    if filename_matches:
        return _wrap(filename_matches, "filename")

    import_matches = [path for path in candidates if _imports_target(path)]
    if import_matches:
        return _wrap(import_matches, "import")

    collected = _pytest_collected_test_files(test_dir)
    if not collected:
        return []

    filename_matches = [
        path
        for path in collected
        if (
            path.stem == f"test_{mod_short}"
            or path.stem.startswith(f"test_{mod_short}_")
            or path.stem == f"{mod_short}_test"
        )
    ]
    if filename_matches:
        return _wrap(filename_matches, "pytest_collection")

    import_matches = [path for path in collected if _imports_target(path)]
    if import_matches:
        return _wrap(import_matches, "pytest_collection")

    return []
def _collect_pytest_nodeids(test_files: list[Path]) -> dict[Path, list[str]]:
    """Collect node IDs for pytest test files when available."""
    if not test_files:
        return {}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:ordeal",
                *[str(f) for f in test_files],
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if completed.returncode not in (0, 1, 5):
        return {}

    results: dict[Path, list[str]] = {}
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("=") or "collected " in line:
            continue
        path_text = line.split("::", 1)[0]
        if not path_text.endswith(".py"):
            continue
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        results.setdefault(candidate, []).append(line)
    return results
def _generated_test_path(module: str) -> Path:
    """Return the generated migrated-test path for *module*."""
    mod_short = module.rsplit(".", 1)[-1]
    return Path(".ordeal") / f"test_{mod_short}_migrated.py"
def _audit_target_cache_key(module: str, target_specs: Sequence[Any] | None = None) -> str:
    """Return a stable cache key for a module plus optional object targets."""
    if not target_specs:
        return module

    serial: list[dict[str, Any]] = []
    for spec in target_specs:
        if isinstance(spec, str):
            serial.append({"target": spec})
            continue
        serial.append(
            {
                "target": str(getattr(spec, "target", "")),
                "factory": getattr(spec, "factory", None),
                "setup": getattr(spec, "setup", None),
                "methods": list(getattr(spec, "methods", [])),
                "include_private": bool(getattr(spec, "include_private", False)),
            }
        )
    digest = hashlib.sha256(json.dumps(serial, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{module}::{digest}"
def _audit_cache_path(module: str) -> Path:
    """Return the on-disk cache path for *module*."""
    safe = module.replace(".", "_").replace(":", "_")
    return Path(".ordeal") / "audit" / f"{safe}.json"
def _hash_file_if_exists(hasher: "hashlib._Hash", path: Path) -> None:
    """Add a file's path and contents to *hasher* when it exists."""
    if not path.exists() or not path.is_file():
        return
    hasher.update(str(path.resolve()).encode("utf-8"))
    hasher.update(path.read_bytes())
