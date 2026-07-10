from __future__ import annotations
# ruff: noqa
def parse_bug_benchmark_manifest(path: str) -> tuple[BugBenchmarkSpec, ...]:
    """Parse a benchmark manifest for public/private bug and control cases."""
    manifest_path = Path(path)
    parse_bug_benchmark_certification_policy(path)
    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)

    defaults = data.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("[defaults] must be a table")

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Benchmark manifest must define at least one [[cases]] entry")

    cases: list[BugBenchmarkSpec] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("Each [[cases]] entry must be a table")
        name = _case_string(raw_case, defaults, "name", required=True)
        assert name is not None
        module = _case_string(raw_case, defaults, "module", required=True)
        assert module is not None
        protocol = _case_string(raw_case, defaults, "protocol") or "scan"
        if protocol != "scan":
            raise ValueError(f"Case {name!r} has unsupported protocol {protocol!r}")
        dataset = _case_string(raw_case, defaults, "dataset") or "custom"
        mode = _case_string(raw_case, defaults, "mode") or "candidate"
        if mode not in {"candidate", "real_bug", "evidence", "coverage_gap"}:
            raise ValueError(f"Case {name!r} has unsupported mode {mode!r}")
        tier = _case_string(raw_case, defaults, "tier") or "public"
        workspace = _case_string(raw_case, defaults, "workspace")
        project = _case_string(raw_case, defaults, "project")
        bug_id = _case_string(raw_case, defaults, "bug_id")
        expected_outcome = _case_string(raw_case, defaults, "expected_outcome") or "bug"
        if expected_outcome not in {"bug", "clean"}:
            raise ValueError(
                f"Case {name!r} has unsupported expected_outcome {expected_outcome!r}"
            )
        selection_reason = _case_string(raw_case, defaults, "selection_reason", required=True)
        oracle_source = _case_string(raw_case, defaults, "oracle_source", required=True)
        evidence_level = _case_string(raw_case, defaults, "evidence_level", required=True)
        saturation_risk = _case_string(raw_case, defaults, "saturation_risk") or "unknown"
        if saturation_risk not in {"public", "private", "unknown"}:
            raise ValueError(f"Case {name!r} has unsupported saturation_risk {saturation_risk!r}")
        allowed_for_optimization = _case_bool(
            raw_case,
            defaults,
            "allowed_for_optimization",
            default=False,
        )
        requires_python = _case_string(raw_case, defaults, "requires_python")
        python_version = _case_string(raw_case, defaults, "python_version")
        if requires_python and python_version:
            raise ValueError(
                f"Case {name!r} cannot define both requires_python and python_version"
            )
        if requires_python:
            try:
                SpecifierSet(requires_python)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"Case {name!r} has invalid requires_python {requires_python!r}"
                ) from exc
        expected_targets = _clean_str_list(
            raw_case.get("expected_targets", defaults.get("expected_targets"))
        )
        expected_files = _clean_str_list(
            raw_case.get("expected_files", defaults.get("expected_files"))
        )
        expected_error_type = _case_string(raw_case, defaults, "expected_error_type")
        expected_error_message = _case_string(raw_case, defaults, "expected_error_message")
        expected_witness_sha256 = _case_string(
            raw_case,
            defaults,
            "expected_witness_sha256",
        )
        if expected_witness_sha256 and not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected_witness_sha256
        ):
            raise ValueError(f"Case {name!r} has invalid expected_witness_sha256")
        if expected_outcome == "clean" and any(
            (expected_error_type, expected_error_message, expected_witness_sha256)
        ):
            raise ValueError(f"Clean case {name!r} cannot define expected bug evidence")
        if not expected_targets and not expected_files:
            raise ValueError(f"Case {name!r} must define expected_targets or expected_files")
        if not workspace and dataset != "bugsinpy":
            raise ValueError(f"Case {name!r} must define workspace unless dataset = 'bugsinpy'")
        if dataset == "bugsinpy" and not workspace and (not project or not bug_id):
            raise ValueError(
                f"Case {name!r} needs workspace or project/bug_id for BugsInPy checkout"
            )
        if dataset == "bugsinpy" and saturation_risk == "private":
            raise ValueError(f"Case {name!r} cannot mark BugsInPy data as private saturation risk")
        if tier == "public" and allowed_for_optimization:
            raise ValueError(
                f"Case {name!r} cannot be tier='public' and allowed_for_optimization=true"
            )
        if saturation_risk == "public" and allowed_for_optimization:
            raise ValueError(
                f"Case {name!r} cannot be public saturation risk and an optimization target"
            )

        reserved = {
            "name",
            "module",
            "dataset",
            "protocol",
            "tier",
            "workspace",
            "project",
            "bug_id",
            "expected_outcome",
            "pair_id",
            "evidence_path",
            "selection_reason",
            "oracle_source",
            "oracle_url",
            "evidence_level",
            "saturation_risk",
            "allowed_for_optimization",
            "harvested_at",
            "fix_commit",
            "failure_command",
            "oracle_python_version",
            "requires_python",
            "python_version",
            "pythonpath",
            "targets",
            "expected_targets",
            "expected_files",
            "expected_error_type",
            "expected_error_message",
            "expected_witness_sha256",
            "max_examples",
            "mode",
            "time_limit",
            "save_artifacts",
            "compile_checkout",
            "notes",
        }
        metadata = {str(key): value for key, value in raw_case.items() if key not in reserved}
        cases.append(
            BugBenchmarkSpec(
                name=name,
                module=module,
                dataset=dataset,
                protocol=protocol,
                tier=tier,
                workspace=workspace,
                project=project,
                bug_id=bug_id,
                expected_outcome=expected_outcome,
                pair_id=_case_string(raw_case, defaults, "pair_id"),
                evidence_path=_case_string(raw_case, defaults, "evidence_path"),
                selection_reason=selection_reason or "",
                oracle_source=oracle_source or "",
                oracle_url=_case_string(raw_case, defaults, "oracle_url"),
                evidence_level=evidence_level or "",
                saturation_risk=saturation_risk,
                allowed_for_optimization=allowed_for_optimization,
                harvested_at=_case_string(raw_case, defaults, "harvested_at"),
                fix_commit=_case_string(raw_case, defaults, "fix_commit"),
                failure_command=_case_string(raw_case, defaults, "failure_command"),
                oracle_python_version=_case_string(raw_case, defaults, "oracle_python_version"),
                requires_python=requires_python,
                python_version=python_version,
                pythonpath=_clean_str_list(raw_case.get("pythonpath", defaults.get("pythonpath"))),
                targets=_clean_str_list(raw_case.get("targets", defaults.get("targets"))),
                expected_targets=expected_targets,
                expected_files=expected_files,
                expected_error_type=expected_error_type,
                expected_error_message=expected_error_message,
                expected_witness_sha256=expected_witness_sha256,
                max_examples=_case_int(raw_case, defaults, "max_examples", default=20),
                mode=mode,
                time_limit=_case_float(raw_case, defaults, "time_limit"),
                save_artifacts=_case_bool(raw_case, defaults, "save_artifacts", default=False),
                compile_checkout=_case_bool(raw_case, defaults, "compile_checkout", default=True),
                notes=_case_string(raw_case, defaults, "notes"),
                metadata=metadata,
            )
        )
    return tuple(cases)
def _prepare_bugsinpy_workspace(
    spec: BugBenchmarkSpec,
    *,
    bugsinpy_root: str | None,
    checkout_root: str | None,
) -> Path:
    """Checkout one BugsInPy case into a local workspace directory."""
    checkout_root_path = Path(checkout_root or ".ordeal/bug-benchmark").resolve()
    checkout_root_path.mkdir(parents=True, exist_ok=True)
    case_name = spec.name.strip()
    case_path = Path(case_name)
    if (
        not case_name
        or case_path.is_absolute()
        or len(case_path.parts) != 1
        or case_name in {".", ".."}
        or "/" in case_name
        or "\\" in case_name
    ):
        raise ValueError(f"Unsafe BugsInPy case name: {spec.name!r}")
    workspace = (checkout_root_path / case_name).resolve()
    try:
        workspace.relative_to(checkout_root_path)
    except ValueError as exc:
        raise ValueError(f"BugsInPy workspace escapes checkout root: {spec.name!r}") from exc
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    checkout_exe = _find_bugsinpy_executable(
        "bugsinpy-checkout",
        bugsinpy_root=bugsinpy_root,
    )
    assert spec.project is not None
    assert spec.bug_id is not None
    subprocess.run(
        [
            checkout_exe,
            "-p",
            spec.project,
            "-v",
            "0",
            "-i",
            spec.bug_id,
            "-w",
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    project_root = _find_bugsinpy_checkout_root(workspace)
    if spec.compile_checkout:
        compile_exe = _find_bugsinpy_executable(
            "bugsinpy-compile",
            bugsinpy_root=bugsinpy_root,
        )
        compile_proc = subprocess.run(
            [compile_exe],
            cwd=str(project_root),
            text=True,
            capture_output=True,
            check=True,
        )
        compile_flag = project_root / "bugsinpy_compile_flag"
        if not compile_flag.exists():
            raise RuntimeError(
                "bugsinpy-compile exited without creating bugsinpy_compile_flag: "
                f"{compile_proc.stderr.strip() or compile_proc.stdout.strip()}"
            )
    return project_root
def _find_bugsinpy_checkout_root(workspace: Path) -> Path:
    """Return the real BugsInPy project root inside *workspace*."""
    manifest = workspace / "bugsinpy_bug.info"
    if manifest.exists():
        return workspace

    candidates = sorted(workspace.rglob("bugsinpy_bug.info"))
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one bugsinpy_bug.info under {workspace}, found {len(candidates)}"
        )
    return candidates[0].parent
def _parse_bugsinpy_info(path: Path) -> dict[str, str]:
    """Parse one BugsInPy `bug.info`-style file into a key/value map."""
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or "=" not in text:
            continue
        key, _, raw_value = text.partition("=")
        data[key.strip()] = raw_value.strip().strip('"')
    return data
def _resolve_pythonpath_entries(
    spec: BugBenchmarkSpec,
    *,
    workspace_path: Path,
) -> tuple[str, ...]:
    """Return absolute `PYTHONPATH` entries for one benchmark case."""
    entries = list(spec.pythonpath)
    bug_info_path = workspace_path / "bugsinpy_bug.info"
    if spec.dataset == "bugsinpy" and bug_info_path.exists():
        pythonpath_value = _parse_bugsinpy_info(bug_info_path).get("pythonpath", "")
        if pythonpath_value:
            entries.extend(item.strip() for item in pythonpath_value.split(";") if item.strip())

    resolved: list[str] = []
    for entry in entries:
        candidate = Path(entry)
        path = candidate if candidate.is_absolute() else (workspace_path / candidate)
        resolved.append(str(path.resolve()))
    return tuple(dict.fromkeys(resolved))
def _workspace_site_packages(workspace_path: Path) -> tuple[str, ...]:
    """Return site-packages paths from a checkout virtualenv when present."""
    candidates = sorted((workspace_path / "env" / "lib").glob("python*/site-packages"))
    windows_site_packages = workspace_path / "env" / "Lib" / "site-packages"
    if windows_site_packages.exists():
        candidates.append(windows_site_packages)
    return tuple(str(path.resolve()) for path in candidates if path.exists())
def _resolve_required_python_version(
    spec: BugBenchmarkSpec,
    *,
    workspace_path: Path,
) -> str | None:
    """Return the required Python version for one benchmark case when known."""
    if spec.python_version:
        return spec.python_version
    bug_info_path = workspace_path / "bugsinpy_bug.info"
    if spec.dataset == "bugsinpy" and bug_info_path.exists():
        return _parse_bugsinpy_info(bug_info_path).get("python_version") or None
    return None
def _major_minor(version: str) -> str:
    """Return the `major.minor` prefix from one version string."""
    pieces = [part for part in str(version).strip().split(".") if part]
    if len(pieces) < 2:
        return str(version).strip()
    return ".".join(pieces[:2])
def _interpreter_version(executable: str) -> str:
    """Return the semantic version reported by *executable*."""
    if Path(executable).resolve() == Path(sys.executable).resolve():
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    proc = subprocess.run(
        [
            executable,
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    version = proc.stdout.strip()
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(
            f"Interpreter {executable!r} reported invalid version {version!r}"
        ) from exc
    return version
def _python_requirement_error(spec: BugBenchmarkSpec, actual_python: str) -> str | None:
    """Return a runner-version mismatch message, if any."""
    if spec.requires_python:
        if Version(actual_python) not in SpecifierSet(spec.requires_python):
            return (
                f"requires Python {spec.requires_python}, "
                f"benchmark runner is using Python {actual_python}"
            )
        return None
    if spec.python_version and _major_minor(spec.python_version) != _major_minor(actual_python):
        return (
            f"requires Python {spec.python_version}, "
            f"benchmark runner is using Python {actual_python}"
        )
    return None
def _resolve_workspace(
    spec: BugBenchmarkSpec,
    *,
    manifest_path: str,
    bugsinpy_root: str | None,
    checkout_root: str | None,
) -> Path:
    """Resolve or materialize the workspace for one benchmark case."""
    if spec.workspace:
        raw = Path(spec.workspace)
        if raw.is_absolute():
            return raw.resolve()
        return (Path(manifest_path).resolve().parent / raw).resolve()
    if spec.dataset == "bugsinpy":
        return _prepare_bugsinpy_workspace(
            spec,
            bugsinpy_root=bugsinpy_root,
            checkout_root=checkout_root,
        )
    raise ValueError(f"Case {spec.name!r} has no workspace")
def _matching_targets(
    findings: list[dict[str, Any]],
    *,
    expected_targets: tuple[str, ...],
) -> tuple[str, ...]:
    """Return matched finding targets for the configured expectations."""
    if not expected_targets:
        return ()
    matches: list[str] = []
    for finding in findings:
        actual = str(finding.get("target") or "").strip()
        if not actual:
            details = finding.get("details") or {}
            module = str(details.get("module") or "").strip()
            function = str(details.get("function") or "").strip()
            if module and function:
                actual = f"{module}.{function}"
            elif module:
                actual = module
        if not actual:
            continue
        if any(_match_target(expected, actual) for expected in expected_targets):
            matches.append(actual)
    return tuple(dict.fromkeys(matches))
def _finding_matches_expected_evidence(
    finding: dict[str, Any],
    *,
    spec: BugBenchmarkSpec,
) -> bool:
    """Return whether a finding matches the case's optional exact oracle values."""
    details = finding.get("details")
    if not isinstance(details, dict):
        details = {}
    if spec.expected_error_message is not None:
        if str(details.get("error", "")) != spec.expected_error_message:
            return False
    if spec.expected_error_type is not None:
        proof_bundle = details.get("proof_bundle")
        if not isinstance(proof_bundle, dict):
            proof_bundle = {}
        failing_path = proof_bundle.get("failing_path")
        if not isinstance(failing_path, dict):
            failing_path = {}
        actual_error_type = str(failing_path.get("error_type") or details.get("error_type") or "")
        if actual_error_type != spec.expected_error_type:
            return False
    if spec.expected_witness_sha256 is not None:
        failing_args = details.get("failing_args")
        if not isinstance(failing_args, dict):
            return False
        if _sha256_payload(failing_args) != spec.expected_witness_sha256:
            return False
    return True
def _matching_files(
    raw_result: dict[str, Any],
    *,
    expected_files: tuple[str, ...],
) -> tuple[str, ...]:
    """Return matched file paths from the raw scan result."""
    if not expected_files:
        return ()
    report = raw_result.get("raw_details", {}).get("report", {})
    details = list(report.get("details", []))
    matches: list[str] = []
    for detail in details:
        source_path = str(detail.get("source_path") or "").strip()
        if not source_path:
            continue
        if any(source_path.endswith(expected.replace("\\", "/")) for expected in expected_files):
            matches.append(source_path)
    return tuple(dict.fromkeys(matches))
