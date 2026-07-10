from __future__ import annotations
# ruff: noqa
def _semantic_bucket(name: str, hint: Any | None) -> str:
    """Infer a coarse semantic bucket for one parameter."""
    lowered = name.lower()
    if any(token in lowered for token in {"path", "file", "dir", "root"}):
        return "path"
    if any(token in lowered for token in {"cmd", "command", "argv", "shell"}):
        return "shell"
    if any(token in lowered for token in {"module", "plugin", "hook", "entrypoint"}):
        return "import"
    if any(
        token in lowered
        for token in {
            "config",
            "pickle",
            "checkpoint",
            "trace",
            "bundle",
            "artifact",
            "blob",
            "frame",
            "manifest",
            "resume",
            "session",
            "snapshot",
            "state",
            "toml",
        }
    ):
        return "serialized"
    if any(
        token in lowered
        for token in {
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
        }
    ):
        return "ipc"
    if "link" in lowered or "symlink" in lowered:
        return "symlink"
    if lowered in {"env", "environ", "headers"} or lowered.endswith("_env"):
        return "mapping"
    if any(token in lowered for token in {"json", "payload", "body", "tool_call"}):
        return "json"
    if any(token in lowered for token in {"message", "response", "request"}):
        return "message"
    if any(token in lowered for token in {"timeout", "count", "size", "port", "index", "status"}):
        return "numeric"
    if hint in {int, float}:
        return "numeric"
    if hint in {dict, list, tuple, set, frozenset}:
        return "collection"
    return "generic"
def _semantic_value_score(bucket: str, value: Any) -> float:
    """Score whether *value* fits a coarse semantic bucket."""
    match bucket:
        case "path":
            return 1.0 if isinstance(value, (str, os.PathLike)) else 0.0
        case "shell":
            return (
                1.0
                if isinstance(value, str)
                or (
                    isinstance(value, (list, tuple))
                    and all(isinstance(item, (str, os.PathLike)) for item in value)
                )
                else 0.0
            )
        case "mapping" | "json":
            return 1.0 if isinstance(value, Mapping) else 0.0
        case "import":
            return (
                1.0 if isinstance(value, str) and ("." in value or value.isidentifier()) else 0.0
            )
        case "serialized":
            return 1.0 if isinstance(value, (bytes, bytearray, memoryview, str, Mapping)) else 0.0
        case "ipc":
            return 1.0 if isinstance(value, (str, bytes, bytearray, memoryview, Mapping)) else 0.0
        case "symlink":
            return 1.0 if isinstance(value, (str, os.PathLike)) else 0.0
        case "message":
            return 1.0 if isinstance(value, (str, Mapping, list, tuple)) else 0.0
        case "numeric":
            return 1.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        case "collection":
            return 1.0 if isinstance(value, (dict, list, tuple, set, frozenset)) else 0.0
        case _:
            return 0.5
def _callable_looks_like_security_shaper(func: Any) -> bool:
    """Return whether *func* looks like a pure shaper rather than a side-effect sink."""
    target = _unwrap(func)
    name = str(getattr(target, "__name__", "")).lower()
    source = ""
    with contextlib.suppress(OSError, TypeError):
        source = inspect.getsource(target).lower()
    tokens = set(re.findall(r"[a-z_]{3,}", f"{name} {source}"))
    if tokens & _SECURITY_SIDE_EFFECT_TOKENS or any(
        token in source for token in _SECURITY_SIDE_EFFECT_TOKENS
    ):
        return False
    return bool(tokens & _SECURITY_SHAPER_TOKENS)
def _security_candidate_inputs(
    func: Any,
    boundary_inputs: Sequence[Mapping[str, Any]],
    sink_categories: Sequence[str],
) -> list[CandidateInput]:
    """Build deterministic, low-side-effect security probes for shaper callables."""
    source_backed_sinks = _source_backed_sink_categories(func, security_focus=True)
    safe_probe_sinks = {"path", "symlink"}
    if (
        not boundary_inputs
        or not _callable_looks_like_security_shaper(func)
        or not set(source_backed_sinks)
        or not set(source_backed_sinks).issubset(safe_probe_sinks)
    ):
        return []
    try:
        sig = inspect.signature(_unwrap(func))
    except Exception:
        return []

    base_kwargs = dict(boundary_inputs[0])
    candidates: list[CandidateInput] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if name not in base_kwargs and param.default is inspect.Signature.empty:
            continue
        bucket = _semantic_bucket(name, safe_get_annotations(_unwrap(func)).get(name))
        if not _semantic_bucket_targets_sink(bucket, source_backed_sinks):
            continue
        probes = _SECURITY_PROBE_VALUES.get(bucket, ())
        for probe in probes:
            kwargs = dict(base_kwargs)
            kwargs[name] = probe
            candidates.append(
                CandidateInput(
                    kwargs=kwargs,
                    origin="security_probe",
                    rationale=(f"security probe for {bucket} trust-boundary handling",),
                )
            )
    return candidates
def _security_base_kwargs(func: Any) -> dict[str, Any] | None:
    """Return one conservative kwargs mapping for deterministic security probes."""
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except Exception:
        return None
    hints = safe_get_annotations(target)
    source_boundaries = _source_boundary_candidates(target)
    doc_boundaries = _docstring_boundary_candidates(target, hints)

    def _fallback_value(name: str, hint: Any | None) -> Any:
        values = list(source_boundaries.get(name, ())) or list(doc_boundaries.get(name, ()))
        if hint is not None:
            values.extend(_boundary_values_for_hint(hint))
        if values:
            return values[0]
        bucket = _semantic_bucket(name, hint)
        if hint in _BOUNDARY_SMOKE_VALUES:
            return _BOUNDARY_SMOKE_VALUES[hint][0]
        match bucket:
            case "path" | "symlink":
                return "artifact.txt"
            case "shell":
                return "echo ordeal"
            case "import":
                return "json"
            case "serialized":
                if hint is dict or get_origin(hint) is dict:
                    return {"checkpoint": "seed-1"}
                if hint in {bytes, bytearray, memoryview}:
                    return b"{}"
                return "{}"
            case "ipc":
                if hint is dict or get_origin(hint) is dict:
                    return {"channel": "ordeal-base"}
                if hint in {bytes, bytearray, memoryview}:
                    return b"ordeal-base"
                return "ordeal-base"
            case "mapping" | "json":
                return {}
            case "message" | "generic":
                return "ok"
            case "numeric":
                return 1
            case "collection":
                return {}
            case _:
                return "ok"

    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if param.default is not inspect.Signature.empty:
            kwargs[name] = param.default
            continue
        kwargs[name] = _fallback_value(name, hints.get(name))
    return kwargs
def _artifact_mutation_probe_values(
    *,
    bucket: str,
    name: str,
    hint: Any | None,
    current_value: Any,
) -> tuple[Any, ...]:
    """Return deterministic artifact/config mutations for one semantic bucket."""
    lowered = name.lower()
    if bucket == "import":
        return _SECURITY_ARTIFACT_MUTATION_VALUES["import_text"]
    if bucket == "serialized":
        if isinstance(current_value, Mapping) or hint is dict or get_origin(hint) is dict:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_mapping"]
        if isinstance(current_value, (bytes, bytearray, memoryview)) or hint in {
            bytes,
            bytearray,
            memoryview,
        }:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_bytes"]
        if any(token in lowered for token in {"config", "manifest", "settings", "toml"}):
            return (
                _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_text"][1],
                *_SECURITY_ARTIFACT_MUTATION_VALUES["serialized_mapping"],
            )
        return _SECURITY_ARTIFACT_MUTATION_VALUES["serialized_text"]
    if bucket == "json":
        if isinstance(current_value, Mapping) or hint is dict or get_origin(hint) is dict:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["json_mapping"]
        return _SECURITY_ARTIFACT_MUTATION_VALUES["json_text"]
    if bucket == "ipc":
        if isinstance(current_value, Mapping) or hint is dict or get_origin(hint) is dict:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["ipc_mapping"]
        if isinstance(current_value, (bytes, bytearray, memoryview)) or hint in {
            bytes,
            bytearray,
            memoryview,
        }:
            return _SECURITY_ARTIFACT_MUTATION_VALUES["ipc_bytes"]
        return _SECURITY_ARTIFACT_MUTATION_VALUES["ipc_text"]
    return ()
def _artifact_mutation_candidate_inputs(
    func: Any,
    boundary_inputs: Sequence[Mapping[str, Any]],
    sink_categories: Sequence[str],
) -> list[CandidateInput]:
    """Build deterministic artifact/config mutation candidates for risky data sinks."""
    source_backed_sinks = _source_backed_sink_categories(func, security_focus=True)
    if not ({"deserialization", "ipc", "import"} & set(source_backed_sinks)):
        return []
    target = _unwrap(func)
    try:
        sig = inspect.signature(target)
    except Exception:
        return []
    hints = safe_get_annotations(target)
    base_kwargs = (
        dict(boundary_inputs[0]) if boundary_inputs else (_security_base_kwargs(target) or {})
    )
    if not base_kwargs:
        return []

    candidates: list[CandidateInput] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if name not in base_kwargs and param.default is inspect.Signature.empty:
            continue
        hint = hints.get(name)
        bucket = _semantic_bucket(name, hint)
        if not _semantic_bucket_targets_sink(bucket, source_backed_sinks):
            continue
        for probe in _artifact_mutation_probe_values(
            bucket=bucket,
            name=name,
            hint=hint,
            current_value=base_kwargs.get(name),
        ):
            kwargs = dict(base_kwargs)
            kwargs[name] = probe
            candidates.append(
                CandidateInput(
                    kwargs=kwargs,
                    origin="artifact_mutation",
                    rationale=(f"artifact/config mutation for {bucket} trust-boundary handling",),
                )
            )
    return candidates
def _likely_contract_profile(
    func: Any,
    *,
    security_focus: bool = False,
    seed_from_tests: bool = True,
    seed_from_fixtures: bool = True,
    seed_from_docstrings: bool = True,
    seed_from_code: bool = True,
    seed_from_call_sites: bool = True,
    treat_any_as_weak: bool = True,
    evidence_index: ProjectEvidenceIndex | None = None,
) -> dict[str, Any]:
    """Infer a weak contract profile from hints, docs, and observed seeds."""
    target = _unwrap(func)
    active_index = (
        evidence_index
        if evidence_index is not None
        and evidence_index.module_name == getattr(target, "__module__", "")
        else None
    )
    cache_key = (
        id(target),
        security_focus,
        seed_from_tests,
        seed_from_fixtures,
        seed_from_docstrings,
        seed_from_code,
        seed_from_call_sites,
        treat_any_as_weak,
    )
    if active_index is not None:
        cached = active_index.cached_contract_profile(cache_key)
        if cached is not None:
            return cached
    module_name, qual_parts, leaf_name = _call_target_parts(target)
    qualname = ".".join([*qual_parts, leaf_name]) if qual_parts else leaf_name
    hints = safe_get_annotations(target)
    doc = (inspect.getdoc(target) or "").lower()

    observed = tuple(
        _seed_examples_for_callable(
            target,
            seed_from_tests=seed_from_tests,
            seed_from_fixtures=seed_from_fixtures,
            seed_from_docstrings=seed_from_docstrings,
            seed_from_code=seed_from_code,
            seed_from_call_sites=seed_from_call_sites,
            evidence_index=active_index,
        )
    )
    observed_types: dict[str, set[str]] = {}
    for example in observed:
        for name, value in example.kwargs.items():
            observed_types.setdefault(name, set()).add(type(value).__name__)

    comparisons = _source_boundary_candidates(target)
    profile_params: dict[str, dict[str, Any]] = {}
    for name in inspect.signature(target).parameters:
        if name in {"self", "cls"}:
            continue
        hint = hints.get(name)
        profile_params[name] = {
            "hint": hint,
            "weak_hint": (hint in {Any, object, None}) if treat_any_as_weak else False,
            "semantic": _semantic_bucket(name, hint),
            "observed_types": sorted(observed_types.get(name, set())),
            "comparison_values": list(comparisons.get(name, [])),
            "doc_mentions": int(name.lower() in doc),
        }

    profile = {
        "module": module_name,
        "qualname": qualname,
        "leaf_name": leaf_name,
        "params": profile_params,
        "seed_examples": list(observed),
        "treat_any_as_weak": treat_any_as_weak,
        "sink_categories": _infer_sink_categories(target, security_focus=security_focus),
        "security_focus": bool(security_focus),
    }
    if active_index is not None:
        active_index.store_contract_profile(cache_key, profile)
    return profile
def _score_contract_fit(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> tuple[float, float, float, list[str]]:
    """Score how well a concrete input matches the inferred contract."""
    params = profile.get("params", {})
    if not kwargs:
        return 1.0, 1.0, 0.0, ["zero-arg callable"]

    fit_scores: list[float] = []
    realism_scores: list[float] = []
    sink_scores: list[float] = []
    reasons: list[str] = []
    seed_examples = list(profile.get("seed_examples", []))
    treat_any_as_weak = bool(profile.get("treat_any_as_weak", True))
    sink_categories = list(profile.get("sink_categories", ()))

    for name, value in kwargs.items():
        meta = params.get(name, {})
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        semantic = str(meta.get("semantic", "generic"))
        observed_types = list(meta.get("observed_types", []))
        comparison_values = list(meta.get("comparison_values", []))

        score = 0.0
        if hint is not None and hint is not Any and not weak_hint:
            if _type_matches(value, hint):
                score += 0.55
                reasons.append(f"{name}: matches type hint")
                if get_origin(hint) is Literal:
                    score += 0.1
                    reasons.append(f"{name}: matches a constrained Literal contract")
            else:
                score -= 0.35
                reasons.append(f"{name}: mismatches type hint")
        elif weak_hint and treat_any_as_weak:
            score += _WEAK_CONTRACT_FIT
            reasons.append(f"{name}: broad or missing type hint")

        if observed_types:
            if type(value).__name__ in observed_types:
                score += 0.25
                reasons.append(f"{name}: matches observed test shape")
            else:
                score -= 0.1
        if comparison_values and value in comparison_values:
            score += 0.1
            reasons.append(f"{name}: reaches boundary mined from code")
        if meta.get("doc_mentions"):
            score += 0.05

        semantic_score = _semantic_value_score(semantic, value)
        if hint is not None and not weak_hint and _type_matches(value, hint):
            semantic_score = max(
                semantic_score,
                0.75 if get_origin(hint) is Literal else 0.6,
            )
        realism_scores.append(semantic_score)
        sink_scores.append(_sink_signal_for_bucket(semantic, sink_categories))
        score += (semantic_score - 0.5) * 0.4
        fit_scores.append(min(max(score, 0.0), 1.0))

    contract_fit = sum(fit_scores) / len(fit_scores)
    if any(getattr(example, "kwargs", None) == dict(kwargs) for example in seed_examples):
        contract_fit = min(contract_fit + 0.15, 1.0)
        reasons.append("matches a concrete seed from tests/docs/code")
    realism = sum(realism_scores) / len(realism_scores) if realism_scores else 0.0
    sink_signal = max(sink_scores, default=0.0)
    return contract_fit, realism, sink_signal, reasons[:6]
def _looks_like_declared_contract_robustness(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    realism: float,
    reachability: float,
) -> bool:
    """Return whether a witness is near the declared contract but still outside it."""
    params = profile.get("params", {})
    strong_mismatch = False
    strong_match = False
    observed_shape = False
    for name, value in kwargs.items():
        meta = params.get(name, {})
        hint = meta.get("hint")
        weak_hint = bool(meta.get("weak_hint"))
        if hint is not None and hint is not Any and not weak_hint:
            if _type_matches(value, hint):
                strong_match = True
            else:
                strong_mismatch = True
        if type(value).__name__ in list(meta.get("observed_types", [])):
            observed_shape = True
        if value in list(meta.get("comparison_values", [])):
            observed_shape = True
    return strong_mismatch and (
        strong_match or observed_shape or realism >= 0.55 or reachability >= 0.75
    )
def _aligned_security_sinks(
    kwargs: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> list[str]:
    """Return sink categories supported by both the callable and the concrete input."""
    params = profile.get("params", {})
    sink_categories = {str(item) for item in profile.get("sink_categories", ())}
    aligned: set[str] = set()
    for name, value in kwargs.items():
        meta = params.get(name, {})
        bucket = str(meta.get("semantic", "generic"))
        if _semantic_value_score(bucket, value) < 0.6:
            continue
        aligned.update(
            sink for sink in _SECURITY_BUCKET_TO_SINKS.get(bucket, ()) if sink in sink_categories
        )
    return sorted(aligned, key=lambda item: (-_SECURITY_SINK_WEIGHTS.get(item, 0.0), item))
