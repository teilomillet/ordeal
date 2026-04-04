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
    fixtures: dict[str, str] = field(default_factory=dict)
    # fixture values are sampled_from specs like "violence,cyber,sexual"
    expected_failures: list[str] = field(default_factory=list)
    # function names where failure is correct behavior (e.g. input validation)
    fixture_registries: list[str] = field(default_factory=list)
    ignore_properties: list[str] = field(default_factory=list)
    ignore_relations: list[str] = field(default_factory=list)
    property_overrides: dict[str, list[str]] = field(default_factory=dict)
    relation_overrides: dict[str, list[str]] = field(default_factory=dict)


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


@dataclass
class OrdealConfig:
    """Top-level configuration loaded from ``ordeal.toml``."""

    explorer: ExplorerConfig = field(default_factory=ExplorerConfig)
    tests: list[TestConfig] = field(default_factory=list)
    scan: list[ScanConfig] = field(default_factory=list)
    report: ReportConfig = field(default_factory=ReportConfig)
    api: APIConfig | None = None
    mutations: MutationConfig | None = None


# ============================================================================
# Validation
# ============================================================================

_VALID_CHECKPOINT_STRATEGIES = {"energy", "uniform", "recent"}
_VALID_REPORT_FORMATS = {"json", "text", "both"}


def _valid_presets() -> frozenset[str]:
    from ordeal.mutations import PRESETS

    return frozenset(PRESETS.keys())


_KNOWN_SECTIONS = {
    "explorer",
    "tests",
    "scan",
    "report",
    "faults",
    "api",
    "mutations",
}


def _fields_of(cls: type) -> set[str]:
    """Derive known keys from a dataclass's fields."""
    from dataclasses import fields as _dc_fields

    return {f.name for f in _dc_fields(cls)}


_KNOWN_EXPLORER_KEYS = _fields_of(ExplorerConfig)
_KNOWN_REPORT_KEYS = _fields_of(ReportConfig)
_KNOWN_MUTATIONS_KEYS = _fields_of(MutationConfig)
_KNOWN_SCAN_KEYS = _fields_of(ScanConfig)
# API and Test configs have extra TOML-only keys not in the dataclass
_KNOWN_API_KEYS = _fields_of(APIConfig) | {"stateful", "mutation_targets", "auto_discover"}
_KNOWN_TEST_KEYS = (_fields_of(TestConfig) - {"class_path"}) | {"class"}


class ConfigError(Exception):
    """Raised when ``ordeal.toml`` is invalid."""


def _warn_unknown_keys(section: str, data: dict, known: set[str]) -> None:
    unknown = set(data.keys()) - known
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )


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
        workers=int(explorer_raw.get("workers", 1)),
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
                fixtures=s.get("fixtures", {}),
                expected_failures=s.get("expected_failures", []),
                fixture_registries=list(s.get("fixture_registries", [])),
                ignore_properties=list(s.get("ignore_properties", [])),
                ignore_relations=list(s.get("ignore_relations", [])),
                property_overrides=dict(s.get("property_overrides", {})),
                relation_overrides=dict(s.get("relation_overrides", {})),
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
        verbose=report_raw.get("verbose", False),
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
        )

    return OrdealConfig(
        explorer=explorer,
        tests=tests,
        scan=scans,
        report=report,
        api=api_cfg,
        mutations=mutations_cfg,
    )
