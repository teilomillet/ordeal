from __future__ import annotations
# ruff: noqa
@functools.lru_cache(maxsize=128)
def _test_seed_examples_cached(
    module_name: str,
    leaf_name: str,
    workspace_root: str,
) -> tuple[SeedExample, ...]:
    """Extract literal call-site seeds for a top-level callable from test files."""
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, leaf_name)
        signature = _signature_without_first_context(func)
    except Exception:
        return ()

    seeds: list[SeedExample] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for path in _callable_seed_files(module_name):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        module_aliases, function_aliases = _import_alias_maps(tree, module_name, leaf_name)
        if not module_aliases and not function_aliases:
            continue
        scopes: list[tuple[ast.AST, list[dict[str, Any]]]] = [(tree, [{}])]
        scopes.extend(
            (node, _function_parametrize_bindings(node))
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for scope, bindings_list in scopes:
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                if not _call_matches_target(
                    node,
                    leaf_name=leaf_name,
                    module_aliases=module_aliases,
                    function_aliases=function_aliases,
                ):
                    continue
                for bindings in bindings_list or [{}]:
                    kwargs = _call_kwargs_from_ast(node, signature=signature, bindings=bindings)
                    if not kwargs:
                        continue
                    dedupe = tuple(sorted((key, repr(value)) for key, value in kwargs.items()))
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    seeds.append(
                        SeedExample(
                            kwargs=kwargs,
                            source="pytest_seed",
                            evidence=f"{path.name}:{getattr(node, 'lineno', '?')}",
                        )
                    )
    return tuple(seeds)
def _test_seed_examples(module_name: str, leaf_name: str) -> tuple[SeedExample, ...]:
    """Extract literal call-site seeds for a top-level callable from test files."""
    return _test_seed_examples_cached(module_name, leaf_name, str(Path.cwd().resolve()))
_test_seed_examples.cache_clear = _test_seed_examples_cached.cache_clear  # type: ignore[attr-defined]
def _source_boundary_candidates(func: Any) -> dict[str, list[Any]]:
    """Mine branch-edge constants from the function source."""
    try:
        source = inspect.getsource(func)
        tree = ast.parse(source)
        signature = _signature_without_first_context(func)
    except Exception:
        return {}

    param_names = {param.name for param in signature.parameters.values()}
    candidates: dict[str, list[Any]] = {name: [] for name in param_names}

    def _add(name: str, value: Any) -> None:
        bucket = candidates.setdefault(name, [])
        if value not in bucket:
            bucket.append(value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left_name = node.left.id if isinstance(node.left, ast.Name) else None
            if left_name in param_names:
                for comparator in node.comparators:
                    if _is_simple_literal_node(comparator):
                        value = _literal_seed_value(comparator)
                        _add(left_name, value)
                        if isinstance(value, int):
                            _add(left_name, value - 1)
                            _add(left_name, value + 1)
                        elif isinstance(value, float):
                            _add(left_name, value - 1.0)
                            _add(left_name, value + 1.0)
            if left_name in param_names and any(
                isinstance(comparator, ast.Constant) and comparator.value is None
                for comparator in node.comparators
            ):
                _add(left_name, None)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            if isinstance(node.operand, ast.Name) and node.operand.id in param_names:
                _add(node.operand.id, "")
                _add(node.operand.id, [])

    return {name: values for name, values in candidates.items() if values}
def _docstring_boundary_candidates(func: Any, hints: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Mine coarse boundary values from parameter-focused docstring hints."""
    doc = (inspect.getdoc(func) or "").lower()
    if not doc:
        return {}

    candidates: dict[str, list[Any]] = {}
    for name in hints:
        lowered = name.lower()
        if lowered not in doc:
            continue
        bucket: list[Any] = []
        if "non-empty" in doc or "nonempty" in doc:
            bucket.extend(["", "x"])
        if "positive" in doc:
            bucket.extend([0, 1])
        if "non-negative" in doc or "nonnegative" in doc:
            bucket.extend([0, 1])
        if "path" in lowered or "file" in lowered:
            bucket.extend(["demo.txt", "demo files/input.txt"])
        if bucket:
            candidates[name] = list(dict.fromkeys(bucket))
    return candidates
def _append_boundary_case(
    cases: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> None:
    """Append *candidate* when it is not already present."""
    if any(existing == candidate for existing in cases):
        return
    cases.append(candidate)
def _boundary_values_for_hint(hint: Any) -> list[Any]:
    """Return deterministic boundary values for common type hints."""
    import types as pytypes

    origin = get_origin(hint)
    if origin is Literal:
        return list(get_args(hint))

    if origin is Union or (hasattr(pytypes, "UnionType") and origin is pytypes.UnionType):
        values: list[Any] = []
        for arg in get_args(hint):
            if arg is type(None):
                values.append(None)
            else:
                values.extend(_boundary_values_for_hint(arg))
        return values

    if origin is list:
        return [[]]
    if origin is tuple:
        return [()]
    if origin is dict:
        return [{}]
    if origin is set:
        return [set()]
    if origin is frozenset:
        return [frozenset()]

    return list(_BOUNDARY_SMOKE_VALUES.get(hint, ()))
def _boundary_smoke_inputs(
    func: Any,
    *,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    seed_examples: Sequence[SeedExample] | None = None,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
) -> list[dict[str, Any]]:
    """Build deterministic boundary and observed inputs for one callable."""
    target = _unwrap(func)
    observed_examples = (
        list(seed_examples)
        if seed_examples is not None
        else list(
            _seed_examples_for_callable(
                target,
                seed_from_tests=seed_from_tests,
                seed_from_fixtures=seed_from_fixtures,
                seed_from_docstrings=seed_from_docstrings,
                seed_from_code=seed_from_code,
                seed_from_call_sites=seed_from_call_sites,
            )
        )
    )
    seeds: list[dict[str, Any]] = [dict(example.kwargs) for example in observed_examples]

    if fixtures and seeds:
        return list(seeds)
    if fixtures:
        return []

    try:
        sig = inspect.signature(target)
    except Exception:
        return seeds
    hints = safe_get_annotations(target)
    source_boundaries = _source_boundary_candidates(target)
    doc_boundaries = _docstring_boundary_candidates(target, hints)

    params = [param for name, param in sig.parameters.items() if name not in {"self", "cls"}]
    if not params:
        return seeds or [{}]

    base_kwargs: dict[str, Any] = {}
    per_param_values: list[tuple[str, list[Any]]] = []
    for param in params:
        values: list[Any] = []
        if param.default is not inspect.Parameter.empty:
            values.append(param.default)
        values.extend(source_boundaries.get(param.name, ()))
        values.extend(doc_boundaries.get(param.name, ()))
        if param.name in hints:
            values.extend(_boundary_values_for_hint(hints[param.name]))
        deduped_values: list[Any] = []
        for value in values:
            if any(existing == value for existing in deduped_values):
                continue
            deduped_values.append(value)
        values = deduped_values
        if not values:
            return seeds
        base_kwargs[param.name] = values[0]
        per_param_values.append((param.name, values))

    cases: list[dict[str, Any]] = list(seeds)
    _append_boundary_case(cases, dict(base_kwargs))
    for name, values in per_param_values:
        for value in values:
            candidate = dict(base_kwargs)
            candidate[name] = value
            _append_boundary_case(cases, candidate)
    return cases
def _candidate_inputs(
    func: Any,
    *,
    fixtures: dict[str, st.SearchStrategy[Any]] | None = None,
    mutate_observed_inputs: bool = True,
    mode: ScanMode = "evidence",
    security_focus: bool = False,
    seed_examples: Sequence[SeedExample] | None = None,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
) -> list[CandidateInput]:
    """Return deterministic candidate inputs with provenance metadata."""
    mode = _normalize_scan_mode(mode)
    target = _unwrap(func)
    observed_examples = (
        list(seed_examples)
        if seed_examples is not None
        else list(
            _seed_examples_for_callable(
                target,
                seed_from_tests=seed_from_tests,
                seed_from_fixtures=seed_from_fixtures,
                seed_from_docstrings=seed_from_docstrings,
                seed_from_code=seed_from_code,
                seed_from_call_sites=seed_from_call_sites,
            )
        )
    )
    candidates: list[CandidateInput] = []
    seen: set[str] = set()

    for example in observed_examples:
        key = repr(sorted(example.kwargs.items()))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            CandidateInput(
                kwargs=dict(example.kwargs),
                origin=example.source,
                rationale=(example.evidence,),
            )
        )

    boundary_inputs = _boundary_smoke_inputs(
        target,
        fixtures=fixtures,
        seed_examples=observed_examples,
        seed_from_tests=seed_from_tests,
        seed_from_fixtures=seed_from_fixtures,
        seed_from_docstrings=seed_from_docstrings,
        seed_from_code=seed_from_code,
        seed_from_call_sites=seed_from_call_sites,
    )

    def _append_boundary_inputs() -> None:
        for kwargs in boundary_inputs:
            key = repr(sorted(kwargs.items()))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(CandidateInput(kwargs=dict(kwargs), origin="boundary"))

    def _append_seed_mutations() -> None:
        if not mutate_observed_inputs:
            return
        try:
            from ordeal.mutagen import mutate_inputs

            rng = __import__("random").Random(42)
            for example in list(candidates):
                if example.origin not in {
                    "test",
                    "fixture",
                    "call_site",
                    "docstring",
                    "source_boundary",
                }:
                    continue
                mutated = mutate_inputs(example.kwargs, rng)
                key = repr(sorted(mutated.items()))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    CandidateInput(
                        kwargs=dict(mutated),
                        origin="seed_mutation",
                        rationale=(*(example.rationale), "mutated from observed test input"),
                    )
                )
        except Exception:
            pass

    if mode == "real_bug" and observed_examples:
        _append_seed_mutations()
        _append_boundary_inputs()
    else:
        _append_boundary_inputs()
        _append_seed_mutations()
    if security_focus:
        sink_categories = _infer_sink_categories(target, security_focus=True)
        for candidate in _security_candidate_inputs(target, boundary_inputs, sink_categories):
            key = repr(sorted(candidate.kwargs.items()))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        for candidate in _artifact_mutation_candidate_inputs(
            target,
            boundary_inputs,
            sink_categories,
        ):
            key = repr(sorted(candidate.kwargs.items()))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

    return candidates
def _type_matches(value: Any, expected: type) -> bool:
    """Check if value matches expected type, handling generics and unions."""
    import types as pytypes

    if expected is type(None):
        return value is None
    origin = get_origin(expected)
    if origin is Literal:
        return any(value == option for option in get_args(expected))
    # Union[str, None] or str | None — check each member
    is_union = origin is Union or (
        hasattr(pytypes, "UnionType") and isinstance(expected, pytypes.UnionType)
    )
    if is_union:
        args = get_args(expected)
        return any(_type_matches(value, a) for a in args)
    if origin is not None:
        # list[int] → check isinstance(value, list)
        return isinstance(value, origin)
    try:
        return isinstance(value, expected)
    except TypeError:
        return True  # can't check, assume ok
_DOC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "for",
    "from",
    "have",
    "into",
    "must",
    "same",
    "that",
    "the",
    "this",
    "when",
    "with",
}
def _documented_precondition_failure(
    func: Any,
    exc: Exception,
    kwargs: dict[str, Any],
    *,
    expected_patterns: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Return a detail dict when *exc* matches a documented precondition."""
    doc = inspect.getdoc(func) or ""
    lowered_doc = doc.lower()

    exc_name = type(exc).__name__
    exc_name_lower = exc_name.lower()

    message = str(exc)
    lowered_message = message.lower()
    message_tokens = {
        token
        for token in re.findall(r"[a-z_]{4,}", lowered_message)
        if token not in _DOC_STOPWORDS
    }
    doc_tokens = set(re.findall(r"[a-z_]{4,}", lowered_doc))
    param_names = {name.lower() for name in kwargs}
    explicit_patterns = [str(item).strip().lower() for item in expected_patterns or () if item]

    doc_match = (
        "raise" in lowered_doc
        and exc_name_lower in lowered_doc
        and ((message_tokens & doc_tokens) or (param_names & doc_tokens))
    )
    explicit_match = any(
        pattern == exc_name_lower
        or pattern in lowered_message
        or pattern in lowered_doc
        or pattern in param_names
        for pattern in explicit_patterns
    )

    if not doc_match and not explicit_match:
        return None

    summary = f"expected precondition failure: {exc_name}: {message}"
    return {
        "kind": "precondition",
        "category": "expected_precondition_failure",
        "summary": summary[:300],
        "error": message[:300],
        "error_type": exc_name,
        "failing_args": dict(kwargs),
        "source": "explicit_annotation" if explicit_match and not doc_match else "docstring",
    }
def _call_target_parts(func: Any) -> tuple[str, tuple[str, ...], str]:
    """Return ``(module_name, qualname_parts, leaf_name)`` for *func*."""
    target = _unwrap(func)
    module_name = getattr(target, "__module__", "")
    qualname = getattr(target, "__qualname__", getattr(target, "__name__", ""))
    parts = tuple(part for part in qualname.split(".") if part and part != "<locals>")
    if not parts:
        return module_name, (), getattr(target, "__name__", "")
    return module_name, parts[:-1], parts[-1]
