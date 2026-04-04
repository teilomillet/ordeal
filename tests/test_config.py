"""Tests for ordeal.config — TOML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ordeal.config import ConfigError, OrdealConfig, load_config


@pytest.fixture
def tmp_toml(tmp_path):
    """Write a TOML string and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "ordeal.toml"
        p.write_text(content)
        return p

    return _write


class TestLoadConfig:
    def test_minimal(self, tmp_toml):
        cfg = load_config(tmp_toml('[explorer]\ntarget_modules = ["myapp"]\n'))
        assert cfg.explorer.target_modules == ["myapp"]
        assert cfg.explorer.max_time == 60.0  # default

    def test_full(self, tmp_toml):
        cfg = load_config(
            tmp_toml("""
[explorer]
target_modules = ["myapp", "myapp.api"]
max_time = 120
seed = 99
max_checkpoints = 512
checkpoint_prob = 0.6
checkpoint_strategy = "recent"
steps_per_run = 30
fault_toggle_prob = 0.5

[[tests]]
class = "tests.test_chaos:CounterChaos"
steps_per_run = 20
swarm = true

[report]
format = "json"
output = "out.json"
traces = true
traces_dir = ".traces"
verbose = true
""")
        )
        assert cfg.explorer.seed == 99
        assert cfg.explorer.checkpoint_strategy == "recent"
        assert len(cfg.tests) == 1
        assert cfg.tests[0].class_path == "tests.test_chaos:CounterChaos"
        assert cfg.tests[0].swarm is True
        assert cfg.report.format == "json"
        assert cfg.report.traces is True

    def test_empty_file(self, tmp_toml):
        cfg = load_config(tmp_toml(""))
        assert isinstance(cfg, OrdealConfig)
        assert cfg.tests == []

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/ordeal.toml")

    def test_unknown_top_section(self, tmp_toml):
        with pytest.raises(ConfigError, match="Unknown top-level section"):
            load_config(tmp_toml("[bogus]\nfoo = 1\n"))

    def test_unknown_explorer_key(self, tmp_toml):
        with pytest.raises(ConfigError, match="Unknown key"):
            load_config(tmp_toml("[explorer]\ntypo_key = 1\n"))

    def test_invalid_checkpoint_strategy(self, tmp_toml):
        with pytest.raises(ConfigError, match="checkpoint_strategy"):
            load_config(tmp_toml('[explorer]\ncheckpoint_strategy = "bad"\n'))

    def test_invalid_report_format(self, tmp_toml):
        with pytest.raises(ConfigError, match="report format"):
            load_config(tmp_toml('[report]\nformat = "xml"\n'))

    def test_explorer_verbose_alias_sets_report_verbose(self, tmp_toml):
        cfg = load_config(tmp_toml("[explorer]\nverbose = true\n"))
        assert cfg.report.verbose is True

    def test_test_missing_class(self, tmp_toml):
        with pytest.raises(ConfigError, match="missing required 'class'"):
            load_config(tmp_toml("[[tests]]\nswarm = true\n"))

    def test_resolve_class(self, tmp_toml):
        cfg = load_config(tmp_toml('[[tests]]\nclass = "tests.test_chaos:CounterChaos"\n'))
        cls = cfg.tests[0].resolve()
        assert cls.__name__ == "CounterChaos"

    def test_scan_config_supports_suppressions_and_registries(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[[scan]]
module = "myapp.scoring"
fixture_registries = ["tests.support.fixtures"]
ignore_properties = ["commutative"]
ignore_relations = ["commutative_composition"]
property_overrides = { score = ["idempotent"] }
relation_overrides = { normalize = ["equivalent"] }
"""
            )
        )
        scan = cfg.scan[0]
        assert scan.fixture_registries == ["tests.support.fixtures"]
        assert scan.ignore_properties == ["commutative"]
        assert scan.ignore_relations == ["commutative_composition"]
        assert scan.property_overrides == {"score": ["idempotent"]}
        assert scan.relation_overrides == {"normalize": ["equivalent"]}

    def test_shared_fixture_registries_section(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[fixtures]
registries = ["tests.support.shared_fixtures", "tests.support.more_fixtures"]

[[scan]]
module = "myapp.scoring"
fixture_registries = ["tests.support.fixtures"]
ignore_properties = ["commutative"]
ignore_relations = ["commutative_composition"]
property_overrides = { score = ["idempotent"] }
relation_overrides = { normalize = ["equivalent"] }
"""
            )
        )
        assert cfg.fixtures.registries == [
            "tests.support.shared_fixtures",
            "tests.support.more_fixtures",
        ]

    def test_audit_section_defaults(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[audit]
modules = ["myapp.scoring", "myapp.pipeline"]
test_dir = "spec"
max_examples = 30
workers = 4
validation_mode = "deep"
write_gaps_dir = "tests/gaps"
include_exploratory_function_gaps = true
require_direct_tests = true
"""
            )
        )
        assert cfg.audit.modules == ["myapp.scoring", "myapp.pipeline"]
        assert cfg.audit.test_dir == "spec"
        assert cfg.audit.max_examples == 30
        assert cfg.audit.workers == 4
        assert cfg.audit.validation_mode == "deep"
        assert cfg.audit.write_gaps_dir == "tests/gaps"
        assert cfg.audit.include_exploratory_function_gaps is True
        assert cfg.audit.require_direct_tests is True

    def test_init_section_defaults(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[init]
target = "myapp"
output_dir = "qa"
ci = true
ci_name = "quality"
install_skill = true
close_gaps = true
gap_output_dir = "qa/gaps"
mutation_preset = "standard"
scan_max_examples = 12
"""
            )
        )
        assert cfg.init.target == "myapp"
        assert cfg.init.output_dir == "qa"
        assert cfg.init.ci is True
        assert cfg.init.ci_name == "quality"
        assert cfg.init.install_skill is True
        assert cfg.init.close_gaps is True
        assert cfg.init.gap_output_dir == "qa/gaps"
        assert cfg.init.mutation_preset == "standard"
        assert cfg.init.scan_max_examples == 12

    def test_invalid_audit_validation_mode(self, tmp_toml):
        with pytest.raises(ConfigError, match="audit.validation_mode"):
            load_config(tmp_toml('[audit]\nvalidation_mode = "slow"\n'))

    def test_invalid_init_mutation_preset(self, tmp_toml):
        with pytest.raises(ConfigError, match="init.mutation_preset"):
            load_config(tmp_toml('[init]\nmutation_preset = "bogus"\n'))
