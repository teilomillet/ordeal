from __future__ import annotations

# ruff: noqa
import importlib
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redefine]
from dataclasses import dataclass, field
from pathlib import Path
# ============================================================================
# Schema
# ============================================================================


@dataclass
class ExplorerConfig:
    """Settings for the coverage-guided Explorer."""

    target_modules: list[str] = field(default_factory=list)
    max_time: float = 60.0
    max_runs: int | None = None
    seed: int = 42
    max_checkpoints: int = 256
    checkpoint_prob: float = 0.4
    checkpoint_strategy: str = "energy"  # "energy" | "uniform" | "recent"
    steps_per_run: int = 50
    fault_toggle_prob: float = 0.3
    workers: int = 0  # 0 = auto (os.cpu_count())
    seed_mutation_respect_strategies: bool = False
    ngram: int = 2  # N-gram depth for edge coverage (1=classic AFL, 2+=path-context)
    rule_swarm: bool = False  # random rule subsets per run (swarm testing for rules)
    rule_timeout: float = 30.0  # per-rule timeout in seconds (0 to disable)


@dataclass
class TestConfig:
    """One ``[[tests]]`` entry — a ChaosTest class to explore."""

    class_path: str  # "module.path:ClassName"
    steps_per_run: int | None = None
    swarm: bool | None = None
    rule_timeout: float | None = None  # override explorer.rule_timeout per test

    def resolve(self) -> type:
        """Import and return the ChaosTest class."""
        module_path, class_name = self.class_path.rsplit(":", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)


@dataclass
class ReportConfig:
    """Output settings for exploration reports."""

    format: str = "text"  # "json" | "text" | "both"
    output: str = "ordeal-report.json"
    traces: bool = False
    traces_dir: str = ".ordeal/traces"
    verbose: bool = False
    corpus_dir: str = ".ordeal/seeds"


@dataclass
class ScanConfig:
    """One ``[[scan]]`` entry — a module to auto-test."""

    module: str
    max_examples: int = 50
    mode: str = "evidence"
    min_contract_fit: float = 0.55
    min_reachability: float = 0.45
    min_realism: float = 0.55
    min_fixture_completeness: float = 0.55
    security_focus: bool = False
    require_replayable: bool = True
    proof_bundles: bool = True
    shell_injection_check: bool = False
    seed_from_tests: bool = True
    seed_from_fixtures: bool = True
    seed_from_docstrings: bool = True
    seed_from_code: bool = True
    seed_from_call_sites: bool = True
    treat_any_as_weak: bool = True
    # Supports pack aliases like ``shell_path_safety`` and
    # ``json_tool_call_normalization``.
    auto_contracts: list[str] = field(default_factory=list)
    ignore_contracts: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    include_private: bool = False
    fixtures: dict[str, str] = field(default_factory=dict)
    # fixture values are sampled_from specs like "violence,cyber,sexual"
    expected_failures: list[str] = field(default_factory=list)
    expected_preconditions: dict[str, list[str]] = field(default_factory=dict)
    # function names where failure is correct behavior (e.g. input validation)
    fixture_registries: list[str] = field(default_factory=list)
    ignore_properties: list[str] = field(default_factory=list)
    ignore_relations: list[str] = field(default_factory=list)
    expected_properties: dict[str, list[str]] = field(default_factory=dict)
    expected_relations: dict[str, list[str]] = field(default_factory=dict)
    property_overrides: dict[str, list[str]] = field(default_factory=dict)
    relation_overrides: dict[str, list[str]] = field(default_factory=dict)
    contract_overrides: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class FixturesConfig:
    """Shared fixture registry configuration for the whole project."""

    registries: list[str] = field(default_factory=list)


@dataclass
class APIConfig:
    """Settings for ``[api]`` OpenAPI chaos testing."""

    schema_url: str | None = None
    app: str | None = None  # "module.path:attr" for ASGI/WSGI app
    wsgi: bool = False
    schema_path: str = "/openapi.json"
    base_url: str | None = None
    faults: list[str] = field(default_factory=list)  # dotted paths to Fault factories
    fault_probability: float = 0.3
    seed: int = 42
    swarm: bool = False
    max_examples: int = 100
    headers: dict[str, str] = field(default_factory=dict)

    def resolve_app(self) -> object | None:
        """Import and return the ASGI/WSGI app object."""
        if self.app is None:
            return None
        module_path, attr_name = self.app.rsplit(":", 1)
        module = importlib.import_module(module_path)
        return getattr(module, attr_name)

    def resolve_faults(self) -> list:
        """Import and call each fault factory path, returning Fault instances."""
        from ordeal.faults import _resolve_target

        results = []
        for path in self.faults:
            parent, attr = _resolve_target(path)
            obj = getattr(parent, attr)
            results.append(obj() if callable(obj) else obj)
        return results


@dataclass
class MutationConfig:
    """Settings for ``[mutations]`` — declarative mutation testing."""

    targets: list[str] = field(default_factory=list)
    preset: str = "standard"
    operators: list[str] | None = None  # mutually exclusive with preset
    workers: int = 0
    threshold: float = 0.0  # 0.0 = no threshold enforcement
    filter_equivalent: bool = True
    equivalence_samples: int = 10
    test_filter: str | None = None  # pytest -k expression
    mutant_timeout: float | None = None  # seconds; abort generation if exceeded
    promote_clusters_only: bool = True
    cluster_min_size: int = 2


@dataclass
class ObjectConfig:
    """One ``[[objects]]`` entry — reusable factory/setup/scenario hooks.

    ``scenarios`` accepts built-in pack names like ``subprocess``, ``sandbox``,
    ``upload_download``, ``http``, and ``state_store`` in addition to symbol paths.
    """

    target: str
    factory: str | None = None
    setup: str | None = None
    state_factory: str | None = None
    teardown: str | None = None
    harness: str = "fresh"
    scenarios: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    include_private: bool = False


@dataclass
class ContractConfig:
    """One ``[[contracts]]`` entry — semantic probes for scan targets."""

    target: str
    checks: list[str] = field(default_factory=list)
    kwargs: dict[str, object] = field(default_factory=dict)
    tracked_params: list[str] = field(default_factory=list)
    protected_keys: list[str] = field(default_factory=list)
    env_param: str | None = None
    phase: str | None = None
    followup_phases: list[str] = field(default_factory=list)
    fault: str | None = None
    handler_name: str | None = None


@dataclass
class AuditTargetConfig:
    """One ``[[audit.targets]]`` entry — a module/class/method target with hooks.

    ``scenarios`` accepts the same built-in pack names as ``[[objects]]``.
    """

    target: str
    factory: str | None = None
    setup: str | None = None
    state_factory: str | None = None
    teardown: str | None = None
    harness: str = "fresh"
    scenarios: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    include_private: bool = False


@dataclass
class AuditConfig:
    """Settings for ``[audit]`` — declarative audit defaults."""

    modules: list[str] = field(default_factory=list)
    targets: list[AuditTargetConfig] = field(default_factory=list)
    test_dir: str = "tests"
    max_examples: int = 20
    workers: int = 1
    validation_mode: str = "fast"
    min_fixture_completeness: float = 0.0
    show_generated: bool = False
    save_generated: str | None = None
    write_gaps_dir: str | None = None
    include_exploratory_function_gaps: bool = False
    require_direct_tests: bool = False


@dataclass
class InitConfig:
    """Settings for ``[init]`` — declarative bootstrap defaults."""

    target: str | None = None
    output_dir: str = "tests"
    ci: bool = False
    ci_name: str = "ordeal"
    install_skill: bool = False
    close_gaps: bool = False
    gap_output_dir: str | None = None
    mutation_preset: str = "essential"
    scan_max_examples: int = 10


@dataclass
class DiffConfig:
    """Settings for revision-isolated ``ordeal diff`` runs."""

    target: str | None = None
    base_ref: str | None = None
    candidate_ref: str = "HEAD"
    max_examples: int = 100
    seed: int = 42
    rtol: float | None = None
    atol: float | None = None
    include_private: bool = False
    fixture_registries: list[str] = field(default_factory=list)
    replay_attempts: int = 2
    save_artifacts: bool = False
    artifact_dir: str = ".ordeal/diff"


@dataclass
class ComposeRequestConfig:
    """One HTTP operation used by the long-lived Compose runner."""

    name: str = "root"
    method: str = "GET"
    path: str = "/"
    headers: dict[str, str] = field(default_factory=dict)
    json_body: object | None = None
    expect_status: list[int] = field(default_factory=list)
    expect_json: dict[str, object] = field(default_factory=dict)
    capture: dict[str, str] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    faultable: bool = True


@dataclass
class ComposeConfig:
    """Settings for ``ordeal explore --runner compose``."""

    base_url: str
    file: str = "compose.yaml"
    project_name: str | None = None
    health_path: str = "/"
    services: list[str] = field(default_factory=list)
    requests: list[ComposeRequestConfig] = field(default_factory=list)
    initial_state: dict[str, object] = field(default_factory=dict)
    max_time: float = 60.0
    steps: int = 50
    seed: int = 42
    fault_probability: float = 0.3
    faults: list[str] = field(default_factory=list)
    delay_seconds: float = 0.5
    request_timeout: float = 5.0
    startup_timeout: float = 30.0
    replay_attempts: int = 3
    workload_mutations: int = 0
    trace_dir: str = ".ordeal/traces"
    keep_running: bool = False


@dataclass
class OrdealConfig:
    """Top-level configuration loaded from ``ordeal.toml``."""

    explorer: ExplorerConfig = field(default_factory=ExplorerConfig)
    tests: list[TestConfig] = field(default_factory=list)
    fixtures: FixturesConfig = field(default_factory=FixturesConfig)
    scan: list[ScanConfig] = field(default_factory=list)
    objects: list[ObjectConfig] = field(default_factory=list)
    contracts: list[ContractConfig] = field(default_factory=list)
    report: ReportConfig = field(default_factory=ReportConfig)
    api: APIConfig | None = None
    mutations: MutationConfig | None = None
    audit: AuditConfig = field(default_factory=AuditConfig)
    init: InitConfig = field(default_factory=InitConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    compose: ComposeConfig | None = None


# ============================================================================
# Validation
# ============================================================================

_VALID_CHECKPOINT_STRATEGIES = {"energy", "uniform", "recent"}
_VALID_REPORT_FORMATS = {"json", "text", "both"}
_VALID_AUDIT_VALIDATION_MODES = {"fast", "deep"}
_VALID_SCAN_MODES = {"coverage_gap", "evidence", "real_bug", "candidate"}
_VALID_COMPOSE_FAULTS = {"kill", "restart", "delay_response", "corrupt_response"}


def _valid_presets() -> frozenset[str]:
    from ordeal.mutations import PRESETS

    return frozenset(PRESETS.keys())


_KNOWN_SECTIONS = {
    "explorer",
    "tests",
    "fixtures",
    "scan",
    "objects",
    "contracts",
    "report",
    "faults",
    "api",
    "mutations",
    "audit",
    "init",
    "diff",
    "compose",
}


def _fields_of(cls: type) -> set[str]:
    """Derive known keys from a dataclass's fields."""
    from dataclasses import fields as _dc_fields

    return {f.name for f in _dc_fields(cls)}


_KNOWN_EXPLORER_KEYS = _fields_of(ExplorerConfig) | {"verbose"}
_KNOWN_REPORT_KEYS = _fields_of(ReportConfig)
_KNOWN_MUTATIONS_KEYS = _fields_of(MutationConfig)
_KNOWN_FIXTURES_KEYS = _fields_of(FixturesConfig)
_KNOWN_SCAN_KEYS = _fields_of(ScanConfig)
_KNOWN_OBJECT_KEYS = _fields_of(ObjectConfig)
_KNOWN_CONTRACT_KEYS = _fields_of(ContractConfig)
_KNOWN_AUDIT_TARGET_KEYS = _fields_of(AuditTargetConfig)
_KNOWN_AUDIT_KEYS = _fields_of(AuditConfig)
_KNOWN_INIT_KEYS = _fields_of(InitConfig)
_KNOWN_DIFF_KEYS = _fields_of(DiffConfig)
_KNOWN_COMPOSE_KEYS = _fields_of(ComposeConfig)
_KNOWN_COMPOSE_REQUEST_KEYS = (_fields_of(ComposeRequestConfig) - {"json_body"}) | {"json"}
# API and Test configs have extra TOML-only keys not in the dataclass
_KNOWN_API_KEYS = _fields_of(APIConfig) | {"stateful", "mutation_targets", "auto_discover"}
_KNOWN_TEST_KEYS = (_fields_of(TestConfig) - {"class_path"}) | {"class"}
_VALID_OBJECT_HARNESSES = {"fresh", "stateful"}


class ConfigError(Exception):
    """Raised when ``ordeal.toml`` is invalid."""


def _warn_unknown_keys(section: str, data: dict, known: set[str]) -> None:
    unknown = set(data.keys()) - known
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )


def _map_of_lists(value: object, *, key_name: str) -> dict[str, list[str]]:
    """Normalize either ``{name=[...]}`` or ``[...]`` into a dict of string lists."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(name): [str(item) for item in items] for name, items in value.items()}
    if isinstance(value, list):
        return {"*": [str(item) for item in value]}
    raise ConfigError(f"{key_name} must be a list or table, got {type(value).__name__}")


def _compose_statuses(value: object, *, request_index: int) -> list[int]:
    """Normalize one Compose request's expected HTTP status codes."""
    if value is None:
        return []
    raw_statuses = [value] if isinstance(value, int) else value
    if not isinstance(raw_statuses, list) or any(
        not isinstance(item, int) for item in raw_statuses
    ):
        raise ConfigError(f"compose.requests.{request_index}.expect_status must be an int or list")
    statuses = [int(item) for item in raw_statuses]
    if any(item < 100 or item > 599 for item in statuses):
        raise ConfigError(
            f"compose.requests.{request_index}.expect_status values must be between 100 and 599"
        )
    return statuses


def _compose_bool(value: object, *, field_name: str) -> bool:
    """Return a strict TOML boolean for a Compose safety setting."""
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a boolean")
    return value
