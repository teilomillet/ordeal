from __future__ import annotations
# ruff: noqa
_CONTRACT_TAG_ALIASES: dict[str, str] = {
    "cleanup_attempts_all": "lifecycle",
    "teardown_attempts_all": "lifecycle",
    "setup_failure_triggers_teardown": "lifecycle",
    "cleanup_after_cancellation": "lifecycle",
    "lifecycle_attempts_all": "lifecycle",
    "lifecycle_followup": "lifecycle",
    "rollout_cancellation_triggers_cleanup": "lifecycle",
    "shell_safe": "shell",
    "subprocess_argv": "shell",
    "command_arg_stability": "shell",
    "quoted_paths": "path",
    "protected_env_keys": "env",
    "json_roundtrip": "json",
    "http_shape": "http",
    "contract_boundary": "contract_boundary",
}
_COHERENT_BOUNDARY_TAGS = {
    "lifecycle",
    "contract_boundary",
    "shell",
    "path",
    "env",
    "json",
    "http",
    "sql",
}
def _normalize_semantic_tag(value: Any) -> str | None:
    """Normalize a contract or heuristic label into one semantic tag."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().replace("-", "_")
    if not lowered:
        return None
    return _CONTRACT_TAG_ALIASES.get(lowered, lowered)
def _metadata_semantic_tags(metadata: Mapping[str, Any] | None) -> list[str]:
    """Extract explicit semantic tags from contract or harness metadata."""
    if not metadata:
        return []
    tags: list[str] = []

    def _add(value: Any) -> None:
        tag = _normalize_semantic_tag(value)
        if tag:
            tags.append(tag)

    for key in ("contract_kind", "kind", "contract_boundary", "boundary", "boundary_label"):
        _add(metadata.get(key))
    contract_tags = (
        metadata.get("contract_tags") or metadata.get("tags") or metadata.get("explicit_tags")
    )
    if isinstance(contract_tags, str):
        _add(contract_tags)
    elif isinstance(contract_tags, Sequence):
        for item in contract_tags:
            _add(item)
    if any(metadata.get(key) is not None for key in ("phase", "followup_phases", "fault")):
        _add("lifecycle")
    return list(dict.fromkeys(tags))
def _merge_semantic_context(*contexts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge explicit metadata contexts with later contexts taking precedence."""
    merged: dict[str, Any] = {}
    for context in contexts:
        if context:
            merged.update(context)
    return merged
def mutation_contract_context(
    contract_checks: Sequence[Any] | None,
    *,
    harness: str | None = None,
) -> dict[str, Any]:
    """Summarize configured contract checks into mutation-ranking metadata."""
    checks = [check for check in contract_checks or () if getattr(check, "name", None)]
    if not checks and not harness:
        return {}

    names: list[str] = []
    kinds: list[str] = []
    phases: list[str] = []
    followups: list[str] = []
    faults: list[str] = []
    tags: list[str] = []

    for check in checks:
        name = str(getattr(check, "name", "")).strip()
        if name:
            names.append(name)
            tags.append(name)
        metadata = getattr(check, "metadata", {}) or {}
        kind = _normalize_semantic_tag(metadata.get("kind"))
        if kind:
            kinds.append(kind)
            tags.append(kind)
        phase = str(metadata.get("phase", "")).strip()
        if phase:
            phases.append(phase)
        fault = str(metadata.get("fault", "")).strip()
        if fault:
            faults.append(fault)
        raw_followups = metadata.get("followup_phases")
        if isinstance(raw_followups, Sequence) and not isinstance(
            raw_followups, (str, bytes, bytearray)
        ):
            for item in raw_followups:
                followup = str(item).strip()
                if followup:
                    followups.append(followup)

    context: dict[str, Any] = {}
    unique_names = list(dict.fromkeys(names))
    unique_kinds = list(dict.fromkeys(kinds))
    unique_tags = list(dict.fromkeys(tags))
    if len(unique_names) == 1:
        context["contract_name"] = unique_names[0]
    if len(unique_kinds) == 1:
        context["contract_kind"] = unique_kinds[0]
    if unique_tags:
        context["contract_tags"] = unique_tags
    if phases:
        context["phase"] = phases[0]
    if followups:
        context["followup_phases"] = list(dict.fromkeys(followups))
    if faults:
        context["fault"] = faults[0]
    if harness:
        context["harness"] = harness
    return context
def _contract_context_summary(metadata: Mapping[str, Any] | None) -> str | None:
    """Render one compact label for explicit contract/harness metadata."""
    if not metadata:
        return None
    parts: list[str] = []
    for key in ("contract_name", "contract_kind", "harness", "phase"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            if key == "harness":
                parts.append(f"harness={value}")
            elif key == "phase":
                parts.append(f"phase={value}")
            else:
                parts.append(str(value))
    return ", ".join(parts) or None
def _mutant_semantic_tags(
    mutant: Mutant,
    *,
    target: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> list[str]:
    """Infer coarse semantic tags for a surviving mutant."""
    target_bits = f"{target or ''} {mutant.qualname or ''}".lower()
    source_bits = f"{mutant.description} {mutant.source_line}".lower()
    tags: list[str] = []
    tags.extend(_metadata_semantic_tags(metadata or mutant.metadata))
    tags.extend(_target_semantic_tags(target or ""))
    if any(
        token in f"{target_bits} {source_bits}"
        for token in {"cleanup", "teardown", "rollout", "setup", "stop"}
    ):
        tags.append("lifecycle")
    if any(token in target_bits for token in {"shell", "argv", "execute_command", "subprocess"}):
        tags.append("shell")
    if any(token in target_bits for token in {"path", "quote", "cwd", "workdir", "upload"}):
        tags.append("path")
    if any(token in target_bits for token in {"env", "environ", "setdefault", "home", "path="}):
        tags.append("env")
    if any(token in target_bits for token in {"json", "tool_call"}):
        tags.append("json")
    if any(token in target_bits for token in {"http"}):
        tags.append("http")
    if any(token in target_bits for token in {"sql", "select ", "insert ", "update ", "delete "}):
        tags.append("sql")
    if mutant.operator in {"boundary", "comparison", "constant"} or any(
        token in source_bits
        for token in {"<", ">", "<=", ">=", "timeout", "limit", "count", "size"}
    ):
        tags.append("boundary")
    if mutant.operator in {"logical", "negate", "remove_not", "swap_if_else"}:
        tags.append("control_flow")
    if mutant.operator in {"return_none", "delete_statement"}:
        tags.append("behavior")
    if not tags:
        tags.append(mutant.operator)
    return list(dict.fromkeys(tags))
def _semantic_cluster_label(tag: str) -> str:
    """Render one semantic survivor-cluster label."""
    return {
        "lifecycle": "lifecycle contract boundary",
        "contract_boundary": "configured contract boundary",
        "shell": "shell/argv construction",
        "path": "path quoting or normalization",
        "env": "environment shaping",
        "json": "JSON/tool-call normalization",
        "http": "HTTP request shaping",
        "sql": "SQL construction",
        "boundary": "boundary handling",
        "control_flow": "control-flow behavior",
        "behavior": "observable behavior",
    }.get(tag, tag.replace("_", " "))
def _qualname_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Return ``(start, end, qualname)`` ranges for functions and methods."""

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.ranges: list[tuple[int, int, str]] = []

        def _visit_scoped(self, node: ast.AST, name: str) -> None:
            self.stack.append(name)
            end_lineno = int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0)
            lineno = int(getattr(node, "lineno", 0) or 0)
            if lineno > 0 and end_lineno >= lineno:
                self.ranges.append((lineno, end_lineno, ".".join(self.stack)))
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scoped(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scoped(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_scoped(node, node.name)

    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.ranges
def _qualname_for_line(ranges: Sequence[tuple[int, int, str]], line: int) -> str | None:
    """Return the deepest qualname that contains *line*."""
    matches = [item for item in ranges if item[0] <= line <= item[1]]
    if not matches:
        return None
    matches.sort(key=lambda item: ((item[1] - item[0]), -item[0]))
    return matches[0][2]
# Multiple candidate values per type — distinct values so that a != b.
# Each list is ordered: [interior, interior, interior, boundary].
# Consecutive params get different values via index offset.
_TYPE_VALUES: dict[str, list[object]] = {
    "int": [2, 3, 7, 0, -1, 100, 5],
    "float": [2.5, 0.7, 3.14, 0.0, -1.0, 0.001, 100.0],
    "str": ["hello", "world", "abc", "", "x", "a b c"],
    "bool": [True, False, True, False],
    "list": [[1.0, 2.0, 3.0], [4.0, 5.0], [7.0], [], [0.0, 0.0]],
    "dict": [{"a": 1}, {"x": 2, "y": 3}, {}, {"k": 0}],
    "bytes": [b"abc", b"xyz", b"", b"\x00\xff"],
    "None": [None],
    "NoneType": [None],
}
# Legacy single-value mapping used by generate_test_stubs (surviving mutants).
_TYPE_EXAMPLES: dict[str, str] = {
    k: repr(v[0]) if k != "str" else repr(v[0]) for k, v in _TYPE_VALUES.items()
}
def _resolve_signature(target: str) -> tuple[str, str]:
    """Resolve a function's signature into display and call forms.

    Returns ``(sig_str, call_args)`` where *sig_str* is the repr-based
    call string using the first value set, and *call_args* uses the
    first candidate per parameter.

    Falls back to ``("(...)", "...")``) when the function can't be resolved.
    """
    try:
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return "(...)", "..."
        func = _resolved_target_callable(target_spec)
        sig = inspect.signature(func)
    except Exception:
        return "(...)", "..."

    sig_str = str(sig)
    kwargs = _build_kwargs(func, value_set=0)
    call_args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items()) if kwargs else "..."
    return sig_str, call_args
def _build_kwargs(func: object, value_set: int = 0) -> dict[str, object]:
    """Build a dict of example kwargs for *func*.

    *value_set* offsets into the candidate list so each call produces
    distinct inputs.  Parameter index also offsets so a != b.
    """
    sig = inspect.signature(func)  # type: ignore[arg-type]
    hints = safe_get_annotations(func)

    kwargs: dict[str, object] = {}
    param_idx = 0
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        hint = hints.get(name)
        hint_name = getattr(hint, "__name__", str(hint)) if hint else ""
        candidates = _TYPE_VALUES.get(hint_name, [None])
        # Pick a different value for each parameter
        idx = (value_set + param_idx) % len(candidates)
        kwargs[name] = candidates[idx]
        param_idx += 1

    return kwargs
def _build_multiple_kwargs(target: str, n: int = 3) -> list[dict[str, object]]:
    """Build *n* distinct sets of kwargs for a function."""
    try:
        target_spec = _resolve_mutation_target(target)
        if target_spec.leaf_name is None:
            return []
        func = _resolved_target_callable(target_spec)
    except Exception:
        return []
    return [_build_kwargs(func, value_set=i) for i in range(n)]
@dataclass
class MutationResult:
    """Aggregated mutation testing results.

    Key attributes and methods::

        result.score              # 0.625 (kill ratio, 1.0 = all caught)
        result.survived           # list of Mutant objects — test gaps
        result.summary()          # formatted report with gaps + fix guidance
        result.generate_test_stubs()  # Python test file with real signatures

    Per-gap data (each item in ``result.survived``)::

        m.operator      # "arithmetic"
        m.description   # "+ -> -"
        m.location      # "L12:4"
        m.source_line   # "return a + b"
        m.remediation   # what test to write to close this gap

    Metadata::

        result.target           # "myapp.scoring.compute"
        result.operators_used   # ["arithmetic", ...] or None
        result.preset_used      # "standard" or None
    """

    target: str
    mutants: list[Mutant] = field(default_factory=list)
    operators_used: list[str] | None = None
    preset_used: str | None = None
    concern: str | None = None
    contract_context: dict[str, Any] = field(default_factory=dict)
    property_observations: list[dict[str, int | str]] = field(default_factory=list)
    validation_sample_matrix_sha256: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    promote_clusters_only: bool = True
    cluster_min_size: int = 2
    diagnostics: dict[str, int] = field(
        default_factory=lambda: {
            "generated": 0,
            "filtered_ast_equivalent": 0,
            "filtered_runtime_equivalent": 0,
            "filtered_module_equivalent": 0,
            "compilation_failed": 0,
            "skipped_display_method": 0,
            "tested": 0,
        }
    )
def total(self) -> int:
    """Total number of mutants generated."""
    return len(self.mutants)
total.__qualname__ = "MutationResult.total"
MutationResult.total = property(total)
del total
def killed(self) -> int:
    """Number of mutants detected (killed) by the tests."""
    return sum(1 for m in self.mutants if m.killed)
killed.__qualname__ = "MutationResult.killed"
MutationResult.killed = property(killed)
del killed
def survived(self) -> list[Mutant]:
    """Mutants that the tests failed to detect — potential test gaps."""
    return [m for m in self.mutants if not m.killed]
survived.__qualname__ = "MutationResult.survived"
MutationResult.survived = property(survived)
del survived
def kill_attribution(self) -> dict[str, list[Mutant]]:
    """Group killed mutants by the test/property that killed them.

    Returns a dict mapping test names to the mutants they caught.
    Shows which tests carry their weight and which are redundant::

        attr = result.kill_attribution()
        for test, mutants in attr.items():
            print(f"{test}: killed {len(mutants)} mutant(s)")
    """
    groups: dict[str, list[Mutant]] = {}
    for m in self.mutants:
        if not m.killed:
            continue
        property_killers = [
            f"property:{name}" for name in m.metadata.get("killed_by_properties", [])
        ]
        killers = property_killers or ([m.killed_by] if m.killed_by else [])
        for killer in killers:
            groups.setdefault(killer, []).append(m)
    return groups
kill_attribution.__qualname__ = "MutationResult.kill_attribution"
MutationResult.kill_attribution = kill_attribution
del kill_attribution
def score(self) -> float:
    """Kill ratio: 1.0 means every mutant was caught."""
    return self.killed / self.total if self.total > 0 else 1.0
score.__qualname__ = "MutationResult.score"
MutationResult.score = property(score)
del score
def score_text(self) -> str:
    """Exact mutation score rendered as ``killed/total (pct%)``."""
    if self.total <= 0:
        return ""
    return f"{self.killed}/{self.total} ({self.score:.0%})"
score_text.__qualname__ = "MutationResult.score_text"
MutationResult.score_text = property(score_text)
del score_text
def semantic_survivor_clusters(self) -> list[dict[str, Any]]:
    """Group surviving mutants by coarse semantic boundary or sink."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for mutant in self.survived:
        context = _merge_semantic_context(self.contract_context, mutant.metadata)
        tags = _mutant_semantic_tags(mutant, target=self.target, metadata=context)
        key = tags[0]
        owner = str(context.get("owner") or mutant.qualname or self.target)
        bucket = groups.setdefault(
            (owner, key),
            {
                "owner": owner,
                "tag": key,
                "label": _semantic_cluster_label(key),
                "mutants": [],
                "operators": set(),
                "lines": set(),
                "contract_kind": context.get("contract_kind") or context.get("kind"),
                "contract_name": context.get("contract_name") or context.get("name"),
                "harness": context.get("harness"),
                "contract_context": context,
                "coherent_boundary": key in _COHERENT_BOUNDARY_TAGS
                or any(tag in _COHERENT_BOUNDARY_TAGS for tag in tags[:3]),
            },
        )
        bucket["mutants"].append(mutant)
        bucket["operators"].add(mutant.operator)
        bucket["lines"].add(mutant.line)

    clusters = []
    for bucket in groups.values():
        clusters.append(
            {
                "owner": bucket["owner"],
                "tag": bucket["tag"],
                "label": bucket["label"],
                "size": len(bucket["mutants"]),
                "operators": sorted(bucket["operators"]),
                "lines": sorted(bucket["lines"]),
                "mutants": list(bucket["mutants"]),
                "contract_kind": bucket["contract_kind"],
                "contract_name": bucket["contract_name"],
                "harness": bucket["harness"],
                "contract_context": dict(bucket["contract_context"]),
                "coherent_boundary": bool(bucket["coherent_boundary"]),
            }
        )
    return sorted(
        clusters,
        key=lambda item: (-int(item["size"]), str(item["owner"]), str(item["tag"])),
    )
semantic_survivor_clusters.__qualname__ = "MutationResult.semantic_survivor_clusters"
MutationResult.semantic_survivor_clusters = semantic_survivor_clusters
del semantic_survivor_clusters
def promoted_survivor_clusters(self) -> list[dict[str, Any]]:
    """Return survivor clusters strong enough to surface as main gaps."""
    clusters = self.semantic_survivor_clusters()
    if not self.promote_clusters_only:
        return clusters
    return [
        cluster
        for cluster in clusters
        if int(cluster["size"]) >= self.cluster_min_size or bool(cluster["coherent_boundary"])
    ]
promoted_survivor_clusters.__qualname__ = "MutationResult.promoted_survivor_clusters"
MutationResult.promoted_survivor_clusters = promoted_survivor_clusters
del promoted_survivor_clusters
def weakest_killers(self, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return compact weakest-killer metadata ordered by strength."""
    ranked = sorted(
        (
            {
                "test": test_name,
                "kills": len(mutants),
                "operators": sorted({mutant.operator for mutant in mutants}),
            }
            for test_name, mutants in self.kill_attribution().items()
        ),
        key=lambda item: (int(item["kills"]), str(item["test"])),
    )
    if limit is None:
        return ranked
    return ranked[:limit]
weakest_killers.__qualname__ = "MutationResult.weakest_killers"
MutationResult.weakest_killers = weakest_killers
