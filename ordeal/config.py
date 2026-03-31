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
import tomllib
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


@dataclass
class TestConfig:
    """One ``[[tests]]`` entry — a ChaosTest class to explore."""

    class_path: str  # "module.path:ClassName"
    steps_per_run: int | None = None
    swarm: bool | None = None

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


@dataclass
class ScanConfig:
    """One ``[[scan]]`` entry — a module to auto-test."""

    module: str
    max_examples: int = 50
    fixtures: dict[str, str] = field(default_factory=dict)
    # fixture values are sampled_from specs like "violence,cyber,sexual"


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


# Backward-compat alias
SchemathesisConfig = APIConfig


@dataclass
class OrdealConfig:
    """Top-level configuration loaded from ``ordeal.toml``."""

    explorer: ExplorerConfig = field(default_factory=ExplorerConfig)
    tests: list[TestConfig] = field(default_factory=list)
    scan: list[ScanConfig] = field(default_factory=list)
    report: ReportConfig = field(default_factory=ReportConfig)
    api: APIConfig | None = None
    schemathesis: APIConfig | None = None  # legacy alias for api


# ============================================================================
# Validation
# ============================================================================

_VALID_CHECKPOINT_STRATEGIES = {"energy", "uniform", "recent"}
_VALID_REPORT_FORMATS = {"json", "text", "both"}

_KNOWN_SECTIONS = {"explorer", "tests", "scan", "report", "faults", "schemathesis", "api"}
_KNOWN_API_KEYS = {
    "schema_url",
    "app",
    "wsgi",
    "schema_path",
    "base_url",
    "faults",
    "fault_probability",
    "seed",
    "swarm",
    "max_examples",
    "headers",
}
_KNOWN_SCHEMATHESIS_KEYS = _KNOWN_API_KEYS | {"stateful", "mutation_targets"}
_KNOWN_EXPLORER_KEYS = {
    "target_modules",
    "max_time",
    "max_runs",
    "seed",
    "max_checkpoints",
    "checkpoint_prob",
    "checkpoint_strategy",
    "steps_per_run",
    "fault_toggle_prob",
    "workers",
}
_KNOWN_TEST_KEYS = {"class", "steps_per_run", "swarm"}
_KNOWN_REPORT_KEYS = {"format", "output", "traces", "traces_dir", "verbose"}


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

    with open(p, "rb") as f:
        raw = tomllib.load(f)

    # Warn on unknown top-level sections
    for key in raw:
        if key not in _KNOWN_SECTIONS:
            raise ConfigError(f"Unknown top-level section: [{key}]")

    # -- Explorer --
    explorer_raw = raw.get("explorer", {})
    _warn_unknown_keys("explorer", explorer_raw, _KNOWN_EXPLORER_KEYS)

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
            )
        )

    # -- Scan --
    scans: list[ScanConfig] = []
    for i, s in enumerate(raw.get("scan", [])):
        if "module" not in s:
            raise ConfigError(f"[[scan]] entry {i} is missing required 'module' key")
        scans.append(
            ScanConfig(
                module=s["module"],
                max_examples=int(s.get("max_examples", 50)),
                fixtures=s.get("fixtures", {}),
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
    )

    if report.format not in _VALID_REPORT_FORMATS:
        raise ConfigError(
            f"Invalid report format: {report.format!r}. Must be one of: {_VALID_REPORT_FORMATS}"
        )

    # -- API / Schemathesis (optional, mutually exclusive) --
    if "api" in raw and "schemathesis" in raw:
        raise ConfigError(
            "Cannot have both [api] and [schemathesis] sections. "
            "Use [api] (the [schemathesis] name is deprecated)."
        )

    api_cfg: APIConfig | None = None
    schemathesis_cfg: APIConfig | None = None

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

    if "schemathesis" in raw:
        s_raw = raw["schemathesis"]
        _warn_unknown_keys("schemathesis", s_raw, _KNOWN_SCHEMATHESIS_KEYS)
        schemathesis_cfg = APIConfig(
            schema_url=s_raw.get("schema_url"),
            app=s_raw.get("app"),
            wsgi=s_raw.get("wsgi", False),
            schema_path=s_raw.get("schema_path", "/openapi.json"),
            base_url=s_raw.get("base_url"),
            faults=s_raw.get("faults", []),
            fault_probability=float(s_raw.get("fault_probability", 0.3)),
            seed=int(s_raw.get("seed", 42)),
            swarm=s_raw.get("swarm", False),
            max_examples=int(s_raw.get("max_examples", 100)),
            headers=s_raw.get("headers", {}),
        )

    return OrdealConfig(
        explorer=explorer,
        tests=tests,
        scan=scans,
        report=report,
        api=api_cfg,
        schemathesis=schemathesis_cfg,
    )
