"""TOML-driven configuration for ordeal.

Reads ``ordeal.toml`` and returns a typed ``OrdealConfig``.  The config
is the single source of truth for exploration runs — shareable, versionable,
usable by both humans and AI agents.

Minimal example::

    # ordeal.toml
    [explorer]
    target_modules = ["myapp"]
    max_time = 60

    [[tests]]
    class = "tests.test_chaos:MyServiceChaos"

Load it::

    from ordeal.config import load_config
    cfg = load_config()             # reads ./ordeal.toml
    cfg = load_config("ci.toml")    # or a custom path
"""

from __future__ import annotations

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
    workers: int = 1
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


# ============================================================================
# Validation
# ============================================================================

_VALID_CHECKPOINT_STRATEGIES = {"energy", "uniform", "recent"}
_VALID_REPORT_FORMATS = {"json", "text", "both"}
_VALID_AUDIT_VALIDATION_MODES = {"fast", "deep"}
_VALID_SCAN_MODES = {"coverage_gap", "evidence", "real_bug", "candidate"}


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


# ============================================================================
# Loader
# ============================================================================


def load_config(path: str | Path = "ordeal.toml") -> OrdealConfig:
    """Load and validate an ``ordeal.toml`` file.

    Args:
        path: Path to the TOML file (default: ``ordeal.toml`` in cwd).

    Returns:
        A validated :class:`OrdealConfig`.

    Raises:
        FileNotFoundError: If the file does not exist.
        ConfigError: If the file is invalid.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Config file is not valid UTF-8: {p}: {exc}") from exc

    # Warn on unknown top-level sections
    for key in raw:
        if key not in _KNOWN_SECTIONS:
            raise ConfigError(f"Unknown top-level section: [{key}]")

    # -- Explorer --
    explorer_raw = raw.get("explorer", {})
    _warn_unknown_keys("explorer", explorer_raw, _KNOWN_EXPLORER_KEYS)

    ngram_val = int(explorer_raw.get("ngram", 2))
    if ngram_val < 1:
        raise ConfigError(f"explorer.ngram must be >= 1, got {ngram_val}")

    explorer = ExplorerConfig(
        target_modules=explorer_raw.get("target_modules", []),
        max_time=float(explorer_raw.get("max_time", 60.0)),
        max_runs=explorer_raw.get("max_runs"),
        seed=int(explorer_raw.get("seed", 42)),
        max_checkpoints=int(explorer_raw.get("max_checkpoints", 256)),
        checkpoint_prob=float(explorer_raw.get("checkpoint_prob", 0.4)),
        checkpoint_strategy=explorer_raw.get("checkpoint_strategy", "energy"),
        steps_per_run=int(explorer_raw.get("steps_per_run", 50)),
        fault_toggle_prob=float(explorer_raw.get("fault_toggle_prob", 0.3)),
        workers=int(explorer_raw.get("workers", 0)),
        seed_mutation_respect_strategies=bool(
            explorer_raw.get("seed_mutation_respect_strategies", False)
        ),
        ngram=ngram_val,
        rule_swarm=explorer_raw.get("rule_swarm", False),
        rule_timeout=float(explorer_raw.get("rule_timeout", 30.0)),
    )

    if explorer.checkpoint_strategy not in _VALID_CHECKPOINT_STRATEGIES:
        raise ConfigError(
            f"Invalid checkpoint_strategy: {explorer.checkpoint_strategy!r}. "
            f"Must be one of: {_VALID_CHECKPOINT_STRATEGIES}"
        )

    # -- Tests --
    tests: list[TestConfig] = []
    for i, t in enumerate(raw.get("tests", [])):
        _warn_unknown_keys(f"tests.{i}", t, _KNOWN_TEST_KEYS)
        if "class" not in t:
            raise ConfigError(f"[[tests]] entry {i} is missing required 'class' key")
        tests.append(
            TestConfig(
                class_path=t["class"],
                steps_per_run=t.get("steps_per_run"),
                swarm=t.get("swarm"),
                rule_timeout=(float(t["rule_timeout"]) if "rule_timeout" in t else None),
            )
        )

    # -- Shared fixtures --
    fixtures_raw = raw.get("fixtures", {})
    _warn_unknown_keys("fixtures", fixtures_raw, _KNOWN_FIXTURES_KEYS)
    fixtures = FixturesConfig(
        registries=list(fixtures_raw.get("registries", [])),
    )

    # -- Scan --
    scans: list[ScanConfig] = []
    for i, s in enumerate(raw.get("scan", [])):
        _warn_unknown_keys(f"scan.{i}", s, _KNOWN_SCAN_KEYS)
        if "module" not in s:
            raise ConfigError(f"[[scan]] entry {i} is missing required 'module' key")
        scans.append(
            ScanConfig(
                module=s["module"],
                max_examples=int(s.get("max_examples", 50)),
                mode=str(s.get("mode", "evidence")),
                min_contract_fit=float(s.get("min_contract_fit", 0.55)),
                min_reachability=float(s.get("min_reachability", 0.45)),
                min_realism=float(s.get("min_realism", 0.55)),
                min_fixture_completeness=float(s.get("min_fixture_completeness", 0.55)),
                security_focus=bool(s.get("security_focus", False)),
                require_replayable=bool(s.get("require_replayable", True)),
                proof_bundles=bool(s.get("proof_bundles", True)),
                seed_from_tests=bool(s.get("seed_from_tests", True)),
                seed_from_fixtures=bool(s.get("seed_from_fixtures", True)),
                seed_from_docstrings=bool(s.get("seed_from_docstrings", True)),
                seed_from_code=bool(s.get("seed_from_code", True)),
                seed_from_call_sites=bool(s.get("seed_from_call_sites", True)),
                treat_any_as_weak=bool(s.get("treat_any_as_weak", True)),
                auto_contracts=list(s.get("auto_contracts", [])),
                ignore_contracts=list(s.get("ignore_contracts", [])),
                targets=list(s.get("targets", [])),
                include_private=bool(s.get("include_private", False)),
                fixtures=s.get("fixtures", {}),
                expected_failures=s.get("expected_failures", []),
                expected_preconditions=_map_of_lists(
                    s.get("expected_preconditions"), key_name="expected_preconditions"
                ),
                fixture_registries=list(s.get("fixture_registries", [])),
                ignore_properties=list(s.get("ignore_properties", [])),
                ignore_relations=list(s.get("ignore_relations", [])),
                expected_properties=_map_of_lists(
                    s.get("expected_properties"), key_name="expected_properties"
                ),
                expected_relations=_map_of_lists(
                    s.get("expected_relations"), key_name="expected_relations"
                ),
                property_overrides=dict(s.get("property_overrides", {})),
                relation_overrides=dict(s.get("relation_overrides", {})),
                contract_overrides=dict(s.get("contract_overrides", {})),
            )
        )
        scan_cfg = scans[-1]
        if scan_cfg.mode not in _VALID_SCAN_MODES:
            raise ConfigError(
                f"Invalid scan.{i}.mode: {scan_cfg.mode!r}. Must be one of: {_VALID_SCAN_MODES}"
            )
        if not (0.0 <= scan_cfg.min_contract_fit <= 1.0):
            raise ConfigError(
                f"scan.{i}.min_contract_fit must be between 0.0 and 1.0, "
                f"got {scan_cfg.min_contract_fit}"
            )
        if not (0.0 <= scan_cfg.min_reachability <= 1.0):
            raise ConfigError(
                f"scan.{i}.min_reachability must be between 0.0 and 1.0, "
                f"got {scan_cfg.min_reachability}"
            )
        if not (0.0 <= scan_cfg.min_realism <= 1.0):
            raise ConfigError(
                f"scan.{i}.min_realism must be between 0.0 and 1.0, got {scan_cfg.min_realism}"
            )
        if not (0.0 <= scan_cfg.min_fixture_completeness <= 1.0):
            raise ConfigError(
                "scan."
                f"{i}.min_fixture_completeness must be between 0.0 and 1.0, "
                f"got {scan_cfg.min_fixture_completeness}"
            )

    # -- Objects --
    object_cfgs: list[ObjectConfig] = []
    for i, obj_raw in enumerate(raw.get("objects", [])):
        _warn_unknown_keys(f"objects.{i}", obj_raw, _KNOWN_OBJECT_KEYS)
        if "target" not in obj_raw:
            raise ConfigError(f"[[objects]] entry {i} is missing required 'target' key")
        object_cfgs.append(
            ObjectConfig(
                target=str(obj_raw["target"]),
                factory=obj_raw.get("factory"),
                setup=obj_raw.get("setup"),
                state_factory=obj_raw.get("state_factory"),
                teardown=obj_raw.get("teardown"),
                harness=str(obj_raw.get("harness", "fresh")),
                scenarios=list(obj_raw.get("scenarios", [])),
                methods=list(obj_raw.get("methods", [])),
                include_private=bool(obj_raw.get("include_private", False)),
            )
        )
        object_cfg = object_cfgs[-1]
        if object_cfg.harness not in _VALID_OBJECT_HARNESSES:
            raise ConfigError(
                f"Invalid objects.{i}.harness: {object_cfg.harness!r}. "
                f"Must be one of: {_VALID_OBJECT_HARNESSES}"
            )

    # -- Contracts --
    contract_cfgs: list[ContractConfig] = []
    for i, contract_raw in enumerate(raw.get("contracts", [])):
        _warn_unknown_keys(f"contracts.{i}", contract_raw, _KNOWN_CONTRACT_KEYS)
        if "target" not in contract_raw:
            raise ConfigError(f"[[contracts]] entry {i} is missing required 'target' key")
        contract_cfgs.append(
            ContractConfig(
                target=str(contract_raw["target"]),
                checks=list(contract_raw.get("checks", [])),
                kwargs=dict(contract_raw.get("kwargs", {})),
                tracked_params=list(contract_raw.get("tracked_params", [])),
                protected_keys=list(contract_raw.get("protected_keys", [])),
                env_param=contract_raw.get("env_param"),
                phase=contract_raw.get("phase"),
                followup_phases=list(contract_raw.get("followup_phases", [])),
                fault=contract_raw.get("fault"),
                handler_name=contract_raw.get("handler_name"),
            )
        )

    # -- Report --
    report_raw = raw.get("report", {})
    _warn_unknown_keys("report", report_raw, _KNOWN_REPORT_KEYS)

    report = ReportConfig(
        format=report_raw.get("format", "text"),
        output=report_raw.get("output", "ordeal-report.json"),
        traces=report_raw.get("traces", False),
        traces_dir=report_raw.get("traces_dir", ".ordeal/traces"),
        verbose=report_raw.get("verbose", explorer_raw.get("verbose", False)),
        corpus_dir=report_raw.get("corpus_dir", ".ordeal/seeds"),
    )

    if report.format not in _VALID_REPORT_FORMATS:
        raise ConfigError(
            f"Invalid report format: {report.format!r}. Must be one of: {_VALID_REPORT_FORMATS}"
        )

    # -- API (optional) --
    api_cfg: APIConfig | None = None

    if "api" in raw:
        a_raw = raw["api"]
        _warn_unknown_keys("api", a_raw, _KNOWN_API_KEYS)
        api_cfg = APIConfig(
            schema_url=a_raw.get("schema_url"),
            app=a_raw.get("app"),
            wsgi=a_raw.get("wsgi", False),
            schema_path=a_raw.get("schema_path", "/openapi.json"),
            base_url=a_raw.get("base_url"),
            faults=a_raw.get("faults", []),
            fault_probability=float(a_raw.get("fault_probability", 0.3)),
            seed=int(a_raw.get("seed", 42)),
            swarm=a_raw.get("swarm", False),
            max_examples=int(a_raw.get("max_examples", 100)),
            headers=a_raw.get("headers", {}),
        )

    # -- Mutations (optional) --
    mutations_cfg: MutationConfig | None = None
    if "mutations" in raw:
        m_raw = raw["mutations"]
        _warn_unknown_keys("mutations", m_raw, _KNOWN_MUTATIONS_KEYS)

        m_preset = m_raw.get("preset")
        m_operators = m_raw.get("operators")

        if m_preset is not None and m_operators is not None:
            raise ConfigError(
                "Cannot specify both 'preset' and 'operators' in [mutations]. "
                "Use one or the other."
            )
        if m_preset is not None and m_preset not in _valid_presets():
            raise ConfigError(
                f"Invalid mutations preset: {m_preset!r}. Must be one of: {_valid_presets()}"
            )

        m_threshold = float(m_raw.get("threshold", 0.0))
        if not (0.0 <= m_threshold <= 1.0):
            raise ConfigError(
                f"mutations.threshold must be between 0.0 and 1.0, got {m_threshold}"
            )

        mutations_cfg = MutationConfig(
            targets=m_raw.get("targets", []),
            preset=m_preset if m_preset is not None else "standard",
            operators=m_operators,
            workers=int(m_raw.get("workers", 1)),
            threshold=m_threshold,
            filter_equivalent=m_raw.get("filter_equivalent", True),
            equivalence_samples=int(m_raw.get("equivalence_samples", 10)),
            test_filter=m_raw.get("test_filter"),
            mutant_timeout=float(mt) if (mt := m_raw.get("mutant_timeout")) is not None else None,
            promote_clusters_only=bool(m_raw.get("promote_clusters_only", True)),
            cluster_min_size=int(m_raw.get("cluster_min_size", 2)),
        )
        if mutations_cfg.cluster_min_size < 1:
            raise ConfigError(
                f"mutations.cluster_min_size must be >= 1, got {mutations_cfg.cluster_min_size}"
            )

    # -- Audit --
    audit_raw = raw.get("audit", {})
    _warn_unknown_keys("audit", audit_raw, _KNOWN_AUDIT_KEYS)
    audit_targets_raw = audit_raw.get("targets", [])
    audit_targets: list[AuditTargetConfig] = []
    for i, target_raw in enumerate(audit_targets_raw):
        _warn_unknown_keys(f"audit.targets.{i}", target_raw, _KNOWN_AUDIT_TARGET_KEYS)
        if "target" not in target_raw:
            raise ConfigError(f"[[audit.targets]] entry {i} is missing required 'target' key")
        audit_targets.append(
            AuditTargetConfig(
                target=str(target_raw["target"]),
                factory=target_raw.get("factory"),
                setup=target_raw.get("setup"),
                state_factory=target_raw.get("state_factory"),
                teardown=target_raw.get("teardown"),
                harness=str(target_raw.get("harness", "fresh")),
                scenarios=list(target_raw.get("scenarios", [])),
                methods=list(target_raw.get("methods", [])),
                include_private=bool(target_raw.get("include_private", False)),
            )
        )
        target_cfg = audit_targets[-1]
        if target_cfg.harness not in _VALID_OBJECT_HARNESSES:
            raise ConfigError(
                f"Invalid audit.targets.{i}.harness: {target_cfg.harness!r}. "
                f"Must be one of: {_VALID_OBJECT_HARNESSES}"
            )
    audit_cfg = AuditConfig(
        modules=list(audit_raw.get("modules", [])),
        targets=audit_targets,
        test_dir=audit_raw.get("test_dir", "tests"),
        max_examples=int(audit_raw.get("max_examples", 20)),
        workers=int(audit_raw.get("workers", 1)),
        validation_mode=audit_raw.get("validation_mode", "fast"),
        min_fixture_completeness=float(audit_raw.get("min_fixture_completeness", 0.0)),
        show_generated=bool(audit_raw.get("show_generated", False)),
        save_generated=audit_raw.get("save_generated"),
        write_gaps_dir=audit_raw.get("write_gaps_dir"),
        include_exploratory_function_gaps=bool(
            audit_raw.get("include_exploratory_function_gaps", False)
        ),
        require_direct_tests=bool(audit_raw.get("require_direct_tests", False)),
    )
    if audit_cfg.validation_mode not in _VALID_AUDIT_VALIDATION_MODES:
        raise ConfigError(
            f"Invalid audit.validation_mode: {audit_cfg.validation_mode!r}. "
            f"Must be one of: {_VALID_AUDIT_VALIDATION_MODES}"
        )
    if not (0.0 <= audit_cfg.min_fixture_completeness <= 1.0):
        raise ConfigError(
            "audit.min_fixture_completeness must be between 0.0 and 1.0, "
            f"got {audit_cfg.min_fixture_completeness}"
        )

    # -- Init --
    init_raw = raw.get("init", {})
    _warn_unknown_keys("init", init_raw, _KNOWN_INIT_KEYS)
    init_cfg = InitConfig(
        target=init_raw.get("target"),
        output_dir=init_raw.get("output_dir", "tests"),
        ci=bool(init_raw.get("ci", False)),
        ci_name=init_raw.get("ci_name", "ordeal"),
        install_skill=bool(init_raw.get("install_skill", False)),
        close_gaps=bool(init_raw.get("close_gaps", False)),
        gap_output_dir=init_raw.get("gap_output_dir"),
        mutation_preset=init_raw.get("mutation_preset", "essential"),
        scan_max_examples=int(init_raw.get("scan_max_examples", 10)),
    )
    if init_cfg.mutation_preset not in _valid_presets():
        raise ConfigError(
            f"Invalid init.mutation_preset: {init_cfg.mutation_preset!r}. "
            f"Must be one of: {_valid_presets()}"
        )

    return OrdealConfig(
        explorer=explorer,
        tests=tests,
        fixtures=fixtures,
        scan=scans,
        objects=object_cfgs,
        contracts=contract_cfgs,
        report=report,
        api=api_cfg,
        mutations=mutations_cfg,
        audit=audit_cfg,
        init=init_cfg,
    )
