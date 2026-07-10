from __future__ import annotations
# ruff: noqa
def harden(
    self,
    extra_tests: list[str],
) -> HardeningResult:
    """Verify that test code actually kills surviving mutants (Meta ACH pattern).

    Takes source strings of test functions written by an AI assistant
    or human, and runs the **three-assurance verification loop** for
    each:

    1. **Buildable** — the test source parses and compiles.
    2. **Valid regression** — the test passes on the original code.
    3. **Hardening** — the test fails (kills) at least one surviving mutant.

    Tests that pass all three assurances are *verified* — they provably
    close a mutation gap.  Tests that fail on the original are *invalid*.
    Tests that pass but don't kill any mutant are *ineffective*.

    Example — AI assistant writes tests, ordeal verifies::

        result = mutate("myapp.scoring.compute", preset="standard")

        hardened = result.harden([
            '''
        def test_boundary():
            from myapp.scoring import compute
            assert compute(0, 5) == 5
            ''',
            '''
        def test_negative():
            from myapp.scoring import compute
            assert compute(-1, 5) == 0
            ''',
        ])

        print(f"Verified: {len(hardened.verified)}")
        print(f"Invalid: {len(hardened.invalid)}")
        print(f"Ineffective: {len(hardened.ineffective)}")

    Args:
        extra_tests: Source strings, each containing one or more
            ``def test_*()`` functions.  The tests should import
            the target function themselves.

    Returns:
        A :class:`HardeningResult` with verified, invalid, and
        ineffective tests.
    """
    if not self.survived:
        return HardeningResult()
    if not extra_tests:
        return HardeningResult()

    # Resolve target function for PatchFault swapping
    target_spec = _resolve_mutation_target(self.target)
    if target_spec.leaf_name is None:
        return HardeningResult()
    func_name = target_spec.leaf_name

    try:
        module = target_spec.module
        _unwrap_func(_resolved_target_callable(target_spec))
    except Exception:
        return HardeningResult()

    # Collect surviving mutants that have source for re-compilation
    swappable: list[tuple[Mutant, Callable]] = []
    for m in self.survived:
        if not m._mutant_source:
            continue
        try:
            tree = ast.parse(m._mutant_source)
            code = compile(tree, f"<mutant:{m.description}>", "exec")
            ns = dict(module.__dict__)
            exec(code, ns)  # noqa: S102
            mf = ns.get(func_name)
            if mf is not None:
                swappable.append((m, mf))
        except Exception:
            continue

    if not swappable:
        return HardeningResult()

    result = HardeningResult()

    for test_source in extra_tests:
        test_source = textwrap.dedent(test_source)

        # 1. Buildable — parse and compile
        try:
            test_tree = ast.parse(test_source)
            test_code = compile(test_tree, "<harden-test>", "exec")
        except Exception:
            result.invalid.append(test_source)
            continue

        # Extract test functions from the compiled source
        test_ns: dict[str, object] = {}
        try:
            exec(test_code, test_ns)  # noqa: S102
        except Exception:
            result.invalid.append(test_source)
            continue

        test_fns = [
            (name, fn) for name, fn in test_ns.items() if name.startswith("test_") and callable(fn)
        ]
        if not test_fns:
            result.invalid.append(test_source)
            continue

        source_verified = False
        for test_name, test_fn in test_fns:
            # 2. Valid regression — passes on original code
            try:
                test_fn()
            except Exception:
                result.invalid.append(test_source)
                source_verified = False
                break

            # 3. Hardening — kills at least one surviving mutant
            kills: list[Mutant] = []
            for mutant, mutant_fn in swappable:
                fault = PatchFault(self.target, lambda orig, mf=mutant_fn: mf)
                fault.activate()
                try:
                    test_fn()
                    # Test passed on mutant — didn't kill it
                except Exception:
                    kills.append(mutant)
                finally:
                    fault.deactivate()

            if kills:
                result.verified.append(
                    VerifiedTest(name=test_name, source=test_source, kills=kills)
                )
                source_verified = True

        if not source_verified and test_source not in result.invalid:
            result.ineffective.append(test_source)

    return result
harden.__qualname__ = "MutationResult.harden"
MutationResult.harden = harden
del harden
# ============================================================================
# Mutation cache — resume support
# ============================================================================


def _module_source_hash(target: str) -> str:
    """Hash that captures everything that could affect mutation results.

    Combines:
    1. **Module source** — any change to the target file
    2. **Test files** — ``tests/test_<module>.py`` and ``tests/conftest.py``
    3. **Lockfile** — ``uv.lock``, ``poetry.lock``, or ``requirements.txt``

    If any of these change, the hash changes and the cache is invalidated.
    This prevents stale results when tests are improved (#1) or
    dependencies are upgraded (#2).
    """
    h = hashlib.sha256()

    # 1. Module source
    module_name = _split_mutation_target(target)[0]
    source_file = None
    try:
        module = importlib.import_module(module_name)
        source_file = getattr(module, "__file__", None)
    except ImportError:
        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin:
            source_file = spec.origin
    if source_file is None:
        raise ValueError(f"Cannot locate source for {target!r}")
    with open(source_file, "rb") as f:
        h.update(f.read())

    # 2. Test files — tests/test_<module>.py, tests/conftest.py, conftest.py
    #    Search from cwd AND from the module's parent directories (covers
    #    both standard and src/ layouts).
    short_name = module_name.split(".")[-1]
    search_roots = [Path.cwd()]
    source_parent = Path(source_file).resolve().parent
    for ancestor in [source_parent, *source_parent.parents]:
        if (ancestor / "tests").is_dir() or (ancestor / "pyproject.toml").exists():
            if ancestor.resolve() not in {r.resolve() for r in search_roots}:
                search_roots.append(ancestor)
            break

    seen_test_files: set[str] = set()
    for root in search_roots:
        # Exact match + prefix glob (test_mutations.py + test_mutations_*.py)
        candidates: list[Path] = [
            root / "tests" / "conftest.py",
            root / "conftest.py",
        ]
        for test_dir in [root / "tests", root]:
            exact = test_dir / f"test_{short_name}.py"
            if exact.exists():
                candidates.append(exact)
            # Glob: test_<module>_*.py (e.g. test_mutations_presets.py)
            if test_dir.is_dir():
                candidates.extend(sorted(test_dir.glob(f"test_{short_name}_*.py")))

        for p in sorted(candidates):
            rp = str(p.resolve())
            if p.exists() and rp not in seen_test_files:
                seen_test_files.add(rp)
                h.update(p.read_bytes())

    # 3. Lockfile — dependency version changes
    for lockfile in ["uv.lock", "poetry.lock", "requirements.txt"]:
        p = Path(lockfile)
        if p.exists():
            h.update(p.read_bytes())
            break  # only use the first one found

    return h.hexdigest()[:16]
def _normalize_extra_mutants(
    extra_mutants: list[str | tuple[str, str]] | None,
) -> list[dict[str, str | None]] | None:
    """Convert extra-mutant inputs into a stable JSON-serializable shape."""
    if extra_mutants is None:
        return None
    normalized: list[dict[str, str | None]] = []
    for item in extra_mutants:
        if isinstance(item, tuple):
            description, source = item
        else:
            description, source = None, item
        normalized.append({"description": description, "source": source})
    return normalized
def _test_fn_fingerprint(test_fn: Callable[[], None]) -> str:
    """Best-effort fingerprint for a custom test callable."""
    source_obj = test_fn
    if not inspect.isfunction(source_obj) and hasattr(source_obj, "__call__"):
        source_obj = source_obj.__call__

    payload: dict[str, object] = {
        "module": getattr(test_fn, "__module__", None),
        "qualname": getattr(test_fn, "__qualname__", getattr(test_fn, "__name__", None)),
        "type": type(test_fn).__qualname__,
    }

    try:
        payload["source"] = textwrap.dedent(inspect.getsource(source_obj))
    except (OSError, TypeError):
        payload["source"] = None

    code = getattr(source_obj, "__code__", None)
    if code is not None:
        payload["bytecode"] = code.co_code.hex()
        payload["consts"] = repr(code.co_consts)
        payload["names"] = list(code.co_names)
        payload["varnames"] = list(code.co_varnames)
        payload["freevars"] = list(code.co_freevars)
    else:
        payload["repr"] = repr(test_fn)

    payload["defaults"] = repr(getattr(test_fn, "__defaults__", None))
    payload["kwdefaults"] = repr(getattr(test_fn, "__kwdefaults__", None))

    closure = getattr(source_obj, "__closure__", None)
    if closure is not None:
        payload["closure"] = [repr(cell.cell_contents) for cell in closure]

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
def _mutation_cache_config_hash(
    *,
    test_fn: Callable[[], None] | None,
    test_filter: str | None,
    filter_equivalent: bool,
    equivalence_samples: int,
    extra_mutants: list[str | tuple[str, str]] | None,
    llm: Callable[[str], str] | None,
    llm_equivalence: bool,
    concern: str | None,
    mutant_timeout: float | None,
    disk_mutation: bool,
    contract_context: Mapping[str, Any] | None = None,
) -> str | None:
    """Hash the mutation settings that materially change the result."""
    if llm is not None:
        return None

    payload = {
        "auto_discovered_tests": test_fn is None,
        "test_fn_fingerprint": None if test_fn is None else _test_fn_fingerprint(test_fn),
        "test_filter": test_filter,
        "filter_equivalent": filter_equivalent,
        "equivalence_samples": equivalence_samples,
        "extra_mutants": _normalize_extra_mutants(extra_mutants),
        "llm_equivalence": llm_equivalence,
        "concern": concern,
        "mutant_timeout": mutant_timeout,
        "disk_mutation": disk_mutation,
        "contract_context": dict(contract_context or {}),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
def _cache_path(target: str) -> Path:
    safe = target.replace(".", "_")
    return Path(".ordeal") / "mutate" / f"{safe}.json"
def _mutant_to_dict(m: Mutant) -> dict:
    return {
        "operator": m.operator,
        "description": m.description,
        "line": m.line,
        "col": m.col,
        "site_summary": m.site_summary,
        "killed": m.killed,
        "error": m.error,
        "source_line": m.source_line,
        "killed_by": m.killed_by,
        "qualname": m.qualname,
        "metadata": m.metadata,
    }
def _mutant_from_dict(d: dict) -> Mutant:
    return Mutant(
        operator=d["operator"],
        description=d["description"],
        line=d.get("line", 0),
        col=d.get("col", 0),
        killed=d.get("killed", False),
        error=d.get("error"),
        source_line=d.get("source_line", ""),
        killed_by=d.get("killed_by"),
        qualname=d.get("qualname"),
        metadata=dict(d.get("metadata", {})),
    )
def _save_cache(target: str, result: MutationResult, module_hash: str, config_hash: str) -> None:
    """Persist a mutation result to .ordeal/mutate/<target>.json."""
    data = {
        "target": target,
        "module_source_hash": module_hash,
        "config_hash": config_hash,
        "preset_used": result.preset_used,
        "operators_used": result.operators_used,
        "concern": result.concern,
        "contract_context": result.contract_context,
        "property_observations": result.property_observations,
        "validation_sample_matrix_sha256": result.validation_sample_matrix_sha256,
        "mutants": [_mutant_to_dict(m) for m in result.mutants],
        "timings": result.timings,
        "diagnostics": result.diagnostics,
        "timestamp": time.time(),
    }
    p = _cache_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.rename(p)  # atomic on POSIX
def _load_cache(
    target: str,
    module_hash: str,
    preset: str | None,
    operators: list[str] | None,
    config_hash: str | None,
) -> MutationResult | None:
    """Load cached mutation result if valid.

    Returns ``None`` (cache miss) when:
    - No cache file exists
    - Module source hash changed (any code modification)
    - Preset or operators changed (different mutation config)
    """
    p = _cache_path(target)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Validate: same source + same config
    if data.get("module_source_hash") != module_hash:
        return None
    if data.get("preset_used") != preset:
        return None
    if data.get("operators_used") != operators:
        return None
    if config_hash is None or data.get("config_hash") != config_hash:
        return None

    result = MutationResult(
        target=target,
        operators_used=data.get("operators_used"),
        preset_used=data.get("preset_used"),
        concern=data.get("concern"),
        contract_context=dict(data.get("contract_context", {})),
        property_observations=[dict(item) for item in data.get("property_observations", [])],
        validation_sample_matrix_sha256=data.get("validation_sample_matrix_sha256"),
    )
    result.mutants = [_mutant_from_dict(m) for m in data.get("mutants", [])]
    result.timings = data.get("timings", {})
    result.diagnostics = data.get("diagnostics", {})
    result.diagnostics["cached"] = result.total
    result.diagnostics["retested"] = 0
    return result
@dataclass
class VerifiedTest:
    """A test that provably kills one or more surviving mutants.

    Attributes:
        name: The test function name (e.g. ``"test_boundary"``).
        source: The full test source code.
        kills: Mutants this test kills — the hardening guarantee.
    """

    name: str
    source: str
    kills: list[Mutant] = field(default_factory=list)
@dataclass
class HardeningResult:
    """Result of the hardening verification loop (Meta ACH 3-assurance pattern).

    Three categories:

    - **verified** — tests that pass all 3 assurances: buildable, valid
      regression (passes on original), and hardening (kills at least one
      surviving mutant).  Each has a machine-verified kill guarantee.
    - **invalid** — tests that don't compile or fail on the original code.
    - **ineffective** — tests that pass on the original but don't kill
      any surviving mutant (they test something, just not a gap).
    """

    verified: list[VerifiedTest] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    ineffective: list[str] = field(default_factory=list)

    @property
    def total_kills(self) -> int:
        """Total number of unique mutants killed across all verified tests."""
        seen: set[tuple[str, int, str]] = set()
        for vt in self.verified:
            for m in vt.kills:
                seen.add((m.operator, m.line, m.description))
        return len(seen)

    def summary(self) -> str:
        """Human-readable summary of hardening results."""
        lines = [
            f"Hardening: {len(self.verified)} verified, "
            f"{len(self.invalid)} invalid, "
            f"{len(self.ineffective)} ineffective"
        ]
        if self.verified:
            lines.append(f"  Unique mutants killed: {self.total_kills}")
            for vt in self.verified:
                descs = ", ".join(m.description for m in vt.kills)
                lines.append(f"  {vt.name}: kills {len(vt.kills)} — {descs}")
        return "\n".join(lines)
def _suggest_invariant(target: str, func_name: str) -> str:
    """Suggest an invariant assertion based on function name and return type."""
    try:
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return ""
        func = _resolved_target_callable(target_spec)
        hints = safe_get_annotations(func)
        ret = hints.get("return")
    except Exception:
        ret = None

    name_lower = func_name.lower()

    # Name-based heuristics
    if any(kw in name_lower for kw in ("score", "rate", "ratio", "prob", "confidence")):
        return "from ordeal.invariants import bounded; bounded(0, 1)(result)"
    if any(kw in name_lower for kw in ("distance", "norm", "magnitude", "loss")):
        return "assert result >= 0"
    if any(kw in name_lower for kw in ("embed", "vector", "matrix", "weight")):
        return "from ordeal.invariants import finite; finite(result)"

    # Return-type heuristics
    ret_name = getattr(ret, "__name__", str(ret)) if ret else ""
    if "float" in ret_name:
        return "from ordeal.invariants import finite; finite(result)"
    if "ndarray" in ret_name or "array" in ret_name or "Tensor" in ret_name:
        return "from ordeal.invariants import finite; finite(result)"

    return ""
