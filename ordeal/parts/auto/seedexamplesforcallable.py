from __future__ import annotations
# ruff: noqa
def _seed_examples_for_callable(
    func: Any,
    *,
    seed_from_tests: bool,
    seed_from_fixtures: bool,
    seed_from_docstrings: bool,
    seed_from_code: bool,
    seed_from_call_sites: bool,
    evidence_index: ProjectEvidenceIndex | None = None,
) -> list[SeedExample]:
    """Collect concrete input witnesses from tests, fixtures, docs, and code."""
    target = _unwrap(func)
    module_name = getattr(target, "__module__", "")
    if not module_name:
        return []

    active_index = (
        evidence_index
        if evidence_index is not None and evidence_index.module_name == module_name
        else None
    )
    cache_key = (
        id(target),
        seed_from_tests,
        seed_from_fixtures,
        seed_from_docstrings,
        seed_from_code,
        seed_from_call_sites,
    )
    if active_index is not None:
        cached = active_index.cached_seed_examples(cache_key)
        if cached is not None:
            return list(cached)

    examples: list[SeedExample] = []
    if active_index is None:
        test_files, project_files = _candidate_seed_files(module_name)
    else:
        test_files, project_files = active_index.test_files, active_index.project_files

    try:
        sig = inspect.signature(target)
        param_names = {name for name in sig.parameters if name not in {"self", "cls"}}
    except Exception:
        param_names = set()

    if seed_from_fixtures and param_names:
        fixture_files = [
            Path.cwd() / "conftest.py",
            Path.cwd() / "tests" / "conftest.py",
            *test_files,
        ]
        fixture_values = (
            active_index.fixture_literals(param_names, fixture_files)
            if active_index is not None
            else _fixture_literals_for_params(param_names, fixture_files)
        )
        if fixture_values:
            kwargs = {name: values[0] for name, values in fixture_values.items() if values}
            _append_seed_example(
                examples,
                kwargs=kwargs,
                source="fixture",
                evidence=", ".join(sorted(fixture_values)[:4]),
            )

    if seed_from_tests:
        seed_examples = (
            active_index.call_seed_examples(target, test_files, source="test")
            if active_index is not None
            else _call_seed_examples_from_files(target, test_files, source="test")
        )
        for example in seed_examples:
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )

    if seed_from_docstrings:
        for example in _doctest_seed_examples(target):
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )

    if seed_from_code:
        for example in _source_boundary_examples(target):
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )

    if seed_from_call_sites:
        seed_examples = (
            active_index.call_seed_examples(target, project_files, source="call_site")
            if active_index is not None
            else _call_seed_examples_from_files(target, project_files, source="call_site")
        )
        for example in seed_examples:
            _append_seed_example(
                examples,
                kwargs=example.kwargs,
                source=example.source,
                evidence=example.evidence,
            )
    if active_index is not None:
        active_index.store_seed_examples(cache_key, examples)
    return examples
def _seed_values_for_param(seed_examples: Sequence[SeedExample], name: str) -> list[Any]:
    """Return distinct observed values for one parameter from seed examples."""
    values: list[Any] = []
    for example in seed_examples:
        if name not in example.kwargs:
            continue
        value = example.kwargs[name]
        if any(existing == value for existing in values):
            continue
        values.append(value)
    return values
def _bias_strategies_with_seed_examples(
    strategies: dict[str, st.SearchStrategy[Any]],
    seed_examples: Sequence[SeedExample],
) -> dict[str, st.SearchStrategy[Any]]:
    """Bias inferred strategies toward previously observed concrete values."""
    biased: dict[str, st.SearchStrategy[Any]] = {}
    for name, strategy in strategies.items():
        seed_values = _seed_values_for_param(seed_examples, name)
        if not seed_values:
            biased[name] = strategy
            continue
        biased[name] = st.one_of(st.sampled_from(seed_values), strategy)
    return biased
def _hint_accepts_value(hint: Any, value: Any) -> bool:
    """Return whether a hint provides positive evidence for *value*."""
    if hint in {Any, object, inspect._empty}:
        return True
    if hint is None:
        return value is None
    origin = get_origin(hint)
    if origin is Literal:
        return value in get_args(hint)
    if origin is Union:
        return any(_hint_accepts_value(arg, value) for arg in get_args(hint))
    if hint is type(None):
        return value is None
    if value is None:
        return False
    if origin is not None:
        with contextlib.suppress(TypeError):
            return isinstance(value, origin)
        return True
    with contextlib.suppress(TypeError):
        return isinstance(value, hint)
    return True
def _hint_is_weak(hint: Any) -> bool:
    """Return whether *hint* is too broad to justify arbitrary fuzzing."""
    return hint in {Any, object, inspect._empty} or hint is None
def _contract_assessment(
    func: Any,
    kwargs: dict[str, Any],
    *,
    seed_examples: Sequence[SeedExample],
    treat_any_as_weak: bool,
) -> tuple[float, float, list[str], list[dict[str, str]]]:
    """Score contract fit and reachability for one concrete input."""
    target = _unwrap(func)
    hints = safe_get_annotations(target)
    reasons: list[str] = []
    evidence: list[dict[str, str]] = []
    fit = 0.0
    reachability = 0.0

    for example in seed_examples:
        if example.kwargs == kwargs:
            weight = {
                "test": 0.45,
                "fixture": 0.4,
                "call_site": 0.35,
                "docstring": 0.25,
                "source_boundary": 0.25,
            }.get(example.source, 0.2)
            fit += weight
            reachability += weight
            evidence.append(
                {
                    "source": example.source,
                    "detail": example.evidence,
                }
            )

    for name, value in kwargs.items():
        hint = hints.get(name, inspect._empty)
        if hint is inspect._empty:
            fit += 0.04
            if treat_any_as_weak:
                reasons.append(f"{name} lacks a precise type hint")
            continue
        if _hint_accepts_value(hint, value):
            if treat_any_as_weak and _hint_is_weak(hint):
                fit += 0.03
                reasons.append(f"{name} uses a broad annotation")
            else:
                fit += 0.14
        else:
            fit -= 0.35
            reasons.append(f"{name} does not match its type hint")
            if value is None:
                reasons.append(f"{name}=None is outside the annotated contract")

    fit = max(0.0, min(fit, 1.0))
    if not evidence:
        reachability += fit * 0.5
    reachability = max(0.0, min(reachability, 1.0))
    return fit, reachability, reasons, evidence
def _sink_category_matches(
    func: Any,
    *,
    security_focus: bool = False,
) -> list[tuple[str, bool, bool]]:
    """Return inferred sink categories plus whether source/param evidence matched."""
    target = _unwrap(func)
    source = ""
    with contextlib.suppress(OSError, TypeError):
        source = inspect.getsource(target).lower()
    try:
        params = {
            name.lower()
            for name in inspect.signature(target).parameters
            if name not in {"self", "cls"}
        }
    except Exception:
        params = set()

    categories: list[tuple[str, bool, bool]] = []
    checks = [
        (
            "shell",
            ("shell=True", "shlex", "execute_command", "subprocess", "command"),
            {"command", "cmd", "argv"},
        ),
        (
            "path",
            ("path", "quote", "upload_content"),
            {"path", "cwd", "workdir", "filename"},
        ),
        (
            "env",
            ("setdefault", "environ", "env_vars", "os.environ"),
            {"env", "env_vars", "environment"},
        ),
        (
            "json_tool_call",
            (
                "json.dumps",
                "json.dump",
                "json.loads",
                "json.load",
                "tool_call",
                "tool_calls",
                "response.json",
                "request.json",
                "model_dump",
                "model_validate_json",
            ),
            {"payload", "tool_call", "tool_calls"},
        ),
        (
            "http",
            (
                "http://",
                "https://",
                "httpx.",
                "requests.",
                "aiohttp",
                "status_code",
                ".headers",
                "content-type",
            ),
            {"headers", "body", "payload", "request"},
        ),
        (
            "sql",
            ("select ", "insert ", "update ", "delete ", "execute("),
            {"query", "sql"},
        ),
        (
            "subprocess",
            ("subprocess", "argv", "run(", "popen("),
            {"argv", "command", "cmd"},
        ),
    ]
    if security_focus:
        checks.extend(
            [
                (
                    "filesystem_write",
                    (
                        "write_text",
                        "write_bytes",
                        ".write(",
                        "mkdir(",
                        "touch(",
                        "save_generated",
                        "write_gaps",
                        "output_dir",
                    ),
                    {"output", "output_dir", "write_gaps", "save_generated", "path", "filename"},
                ),
                (
                    "import",
                    (
                        "importlib",
                        "import_module",
                        "__import__",
                        "module_from_spec",
                        "spec_from_file_location",
                    ),
                    {"module", "module_name", "class_path", "target", "hook", "plugin"},
                ),
                (
                    "deserialization",
                    (
                        "pickle.load",
                        "pickle.loads",
                        "msgpack",
                        "marshal",
                        "cbor",
                        "yaml.load",
                        "yaml.safe_load",
                        "plistlib",
                        "from_bytes",
                        "tomllib.load",
                        "tomllib.loads",
                        "json.loads",
                        "json.load",
                        "literal_eval",
                    ),
                    {
                        "artifact",
                        "blob",
                        "bundle",
                        "checkpoint",
                        "config",
                        "frame",
                        "manifest",
                        "payload",
                        "resume",
                        "session",
                        "snapshot",
                        "state",
                        "trace",
                    },
                ),
                (
                    "ipc",
                    (
                        "shared_memory",
                        "sharedmemory",
                        "multiprocessing",
                        "ring buffer",
                        "checkpoint pool",
                        "socket",
                        "pipe(",
                        "connection",
                        "recv_bytes",
                        "send_bytes",
                        "sharedmemorymanager",
                        "queue(",
                        "mmap",
                    ),
                    {
                        "channel",
                        "checkpoint",
                        "descriptor",
                        "fd",
                        "mailbox",
                        "pipe",
                        "queue",
                        "ring",
                        "segment",
                        "shared_memory",
                        "shm",
                        "sock",
                        "topic",
                    },
                ),
                (
                    "symlink",
                    ("symlink", "readlink", "is_symlink", ".resolve("),
                    {"link", "symlink", "target_path"},
                ),
            ]
        )
    for category, tokens, param_names in checks:
        source_match = any(token in source for token in tokens)
        param_match = bool(params & param_names)
        if category in {"json_tool_call", "http", "sql", "filesystem_write"}:
            matched = source_match
        else:
            matched = source_match or param_match
        if matched:
            categories.append((category, source_match, param_match))
    return categories
def _source_backed_sink_categories(func: Any, *, security_focus: bool = False) -> list[str]:
    """Return sink categories backed by concrete source evidence."""
    return [
        category
        for category, source_match, _param_match in _sink_category_matches(
            func,
            security_focus=security_focus,
        )
        if source_match
    ]
def _infer_sink_categories(func: Any, *, security_focus: bool = False) -> list[str]:
    """Infer semantic sink categories from source and parameter names."""
    categories = [
        category
        for category, _source_match, _param_match in _sink_category_matches(
            func,
            security_focus=security_focus,
        )
    ]
    return categories
def _critical_security_sinks(sink_categories: Sequence[str]) -> list[str]:
    """Return high-risk sink categories in descending weight order."""
    return sorted(
        {
            str(category)
            for category in sink_categories
            if _SECURITY_SINK_WEIGHTS.get(str(category), 0.0) >= 0.9
        },
        key=lambda name: (-_SECURITY_SINK_WEIGHTS.get(name, 0.0), name),
    )
def _semantic_bucket_targets_sink(bucket: str, sink_categories: Sequence[str]) -> bool:
    """Return whether one semantic bucket can reach the inferred sink set."""
    return _sink_signal_for_bucket(bucket, sink_categories) > 0.0
def _proof_bundle_critical_sinks(proof_bundle: Mapping[str, Any] | None) -> list[str] | None:
    """Return explicit critical-sink evidence from one proof bundle when present."""
    if not isinstance(proof_bundle, Mapping):
        return None
    for key in ("impact", "contract_basis"):
        section = proof_bundle.get(key)
        if isinstance(section, Mapping) and "critical_sinks" in section:
            return [str(item) for item in list(section.get("critical_sinks", ()) or ())]
    if "critical_sinks" in proof_bundle:
        return [str(item) for item in list(proof_bundle.get("critical_sinks", ()) or ())]
    return None
def _proof_bundle_replayable(
    proof_bundle: Mapping[str, Any] | None,
    replayable: bool | None,
) -> bool:
    """Return whether replay evidence confirms the proof bundle witness."""
    if isinstance(proof_bundle, Mapping):
        reproduction = proof_bundle.get("reproduction")
        if isinstance(reproduction, Mapping) and reproduction.get("replayable") is not None:
            return bool(reproduction.get("replayable"))
        confidence = proof_bundle.get("confidence_breakdown")
        if isinstance(confidence, Mapping):
            replayability = confidence.get("replayability")
            if isinstance(replayability, (int, float)):
                return replayability >= 1.0
    return bool(replayable)
def _proof_verdict_promoted(
    proof_bundle: Mapping[str, Any] | None,
    *,
    default: bool = False,
) -> bool:
    """Return the explicit proof-bundle promotion verdict when present."""
    if isinstance(proof_bundle, Mapping):
        verdict = proof_bundle.get("verdict")
        if isinstance(verdict, Mapping) and verdict.get("promoted") is not None:
            return bool(verdict.get("promoted"))
    return default
def _contract_violation_promoted(detail: Mapping[str, Any] | None) -> bool:
    """Return whether one contract violation should count as a promoted finding."""
    if not isinstance(detail, Mapping):
        return False
    category = str(detail.get("category") or "")
    if category == "lifecycle_contract":
        return True
    if category != "semantic_contract":
        return False
    return _proof_verdict_promoted(detail.get("proof_bundle"), default=False)
