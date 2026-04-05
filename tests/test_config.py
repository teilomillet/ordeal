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
targets = ["myapp.scoring:Env.build_env_vars"]
include_private = true
fixture_registries = ["tests.support.fixtures"]
ignore_properties = ["commutative"]
ignore_relations = ["commutative_composition"]
property_overrides = { score = ["idempotent"] }
relation_overrides = { normalize = ["equivalent"] }
"""
            )
        )
        scan = cfg.scan[0]
        assert scan.targets == ["myapp.scoring:Env.build_env_vars"]
        assert scan.include_private is True
        assert scan.fixture_registries == ["tests.support.fixtures"]
        assert scan.ignore_properties == ["commutative"]
        assert scan.ignore_relations == ["commutative_composition"]
        assert scan.property_overrides == {"score": ["idempotent"]}
        assert scan.relation_overrides == {"normalize": ["equivalent"]}

    def test_scan_config_supports_precision_controls(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[[scan]]
module = "myapp.scoring"
mode = "real_bug"
min_contract_fit = 0.8
min_reachability = 0.7
min_realism = 0.9
require_replayable = true
proof_bundles = true
seed_from_tests = true
seed_from_fixtures = false
seed_from_docstrings = false
seed_from_code = true
seed_from_call_sites = true
treat_any_as_weak = true
auto_contracts = ["shell_safe", "json_roundtrip"]
"""
            )
        )
        scan = cfg.scan[0]
        assert scan.mode == "real_bug"
        assert scan.min_contract_fit == pytest.approx(0.8)
        assert scan.min_reachability == pytest.approx(0.7)
        assert scan.min_realism == pytest.approx(0.9)
        assert scan.seed_from_fixtures is False
        assert scan.seed_from_docstrings is False
        assert scan.auto_contracts == ["shell_safe", "json_roundtrip"]

    def test_invalid_scan_precision_controls(self, tmp_toml):
        with pytest.raises(ConfigError, match="scan.0.mode"):
            load_config(
                tmp_toml(
                    """
[[scan]]
module = "myapp.scoring"
mode = "bogus"
"""
                )
            )
        with pytest.raises(ConfigError, match="scan.0.min_contract_fit"):
            load_config(
                tmp_toml(
                    """
[[scan]]
module = "myapp.scoring"
min_contract_fit = 1.5
"""
                )
            )

    def test_mutation_config_supports_cluster_promotion(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[mutations]
targets = ["myapp.scoring.compute"]
promote_clusters_only = false
cluster_min_size = 3
"""
            )
        )
        assert cfg.mutations is not None
        assert cfg.mutations.promote_clusters_only is False
        assert cfg.mutations.cluster_min_size == 3

    def test_object_and_contract_config_sections(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[[objects]]
target = "myapp.envs:ComposableEnv"
factory = "tests.support.factories:make_composable_env"
setup = "tests.support.factories:prime_composable_env"
scenarios = ["tests.support.scenarios:disable_network", "tests.support.scenarios:enable_fast_path"]
methods = ["build_env_vars", "post_sandbox_setup"]

[[contracts]]
target = "myapp.envs:ComposableEnv.build_env_vars"
checks = ["shell_safe", "quoted_paths", "protected_env_keys"]
kwargs = { path = "a b", env_vars = { PATH = "/bin", HOME = "/tmp/home" } }
tracked_params = ["path"]
protected_keys = ["PATH", "HOME"]
env_param = "env_vars"
"""
            )
        )

        assert cfg.objects[0].target == "myapp.envs:ComposableEnv"
        assert cfg.objects[0].factory == "tests.support.factories:make_composable_env"
        assert cfg.objects[0].setup == "tests.support.factories:prime_composable_env"
        assert cfg.objects[0].scenarios == [
            "tests.support.scenarios:disable_network",
            "tests.support.scenarios:enable_fast_path",
        ]
        assert cfg.objects[0].methods == ["build_env_vars", "post_sandbox_setup"]
        assert cfg.contracts[0].target == "myapp.envs:ComposableEnv.build_env_vars"
        assert cfg.contracts[0].checks == [
            "shell_safe",
            "quoted_paths",
            "protected_env_keys",
        ]
        assert cfg.contracts[0].tracked_params == ["path"]
        assert cfg.contracts[0].protected_keys == ["PATH", "HOME"]
        assert cfg.contracts[0].env_param == "env_vars"

    def test_contract_config_supports_lifecycle_fields(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[[contracts]]
target = "myapp.envs:ComposableEnv.rollout"
checks = ["lifecycle_followup"]
kwargs = { marker = "demo" }
phase = "rollout"
followup_phases = ["cleanup", "teardown"]
fault = "cancel_rollout"
handler_name = "cleanup_alpha"
"""
            )
        )

        contract = cfg.contracts[0]
        assert contract.target == "myapp.envs:ComposableEnv.rollout"
        assert contract.checks == ["lifecycle_followup"]
        assert contract.kwargs == {"marker": "demo"}
        assert contract.phase == "rollout"
        assert contract.followup_phases == ["cleanup", "teardown"]
        assert contract.fault == "cancel_rollout"
        assert contract.handler_name == "cleanup_alpha"

    def test_audit_target_config_supports_scenarios(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[audit]
modules = ["myapp.scoring"]

[[audit.targets]]
target = "verifiers.envs.experimental.cli_agent_env:CliAgentEnv"
factory = "tests.support.factories:make_cli_agent_env"
setup = "tests.support.factories:prime_cli_agent_env"
scenarios = [
  "tests.support.scenarios:disable_network",
  "tests.support.scenarios:protect_env_keys",
]
methods = ["build_env_vars", "post_sandbox_setup"]
include_private = true
"""
            )
        )

        target = cfg.audit.targets[0]
        assert target.scenarios == [
            "tests.support.scenarios:disable_network",
            "tests.support.scenarios:protect_env_keys",
        ]

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
min_fixture_completeness = 0.5
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
        assert cfg.audit.min_fixture_completeness == 0.5
        assert cfg.audit.write_gaps_dir == "tests/gaps"
        assert cfg.audit.include_exploratory_function_gaps is True
        assert cfg.audit.require_direct_tests is True

    def test_audit_target_config_supports_object_factories(self, tmp_toml):
        cfg = load_config(
            tmp_toml(
                """
[audit]
modules = ["myapp.scoring"]

[[audit.targets]]
target = "verifiers.envs.experimental.cli_agent_env:CliAgentEnv"
factory = "tests.support.factories:make_cli_agent_env"
setup = "tests.support.factories:prime_cli_agent_env"
state_factory = "tests.support.factories:make_cli_agent_state"
teardown = "tests.support.factories:teardown_cli_agent_env"
harness = "stateful"
methods = ["build_env_vars", "post_sandbox_setup"]
include_private = true
"""
            )
        )

        target = cfg.audit.targets[0]
        assert target.target == "verifiers.envs.experimental.cli_agent_env:CliAgentEnv"
        assert target.factory == "tests.support.factories:make_cli_agent_env"
        assert target.setup == "tests.support.factories:prime_cli_agent_env"
        assert target.state_factory == "tests.support.factories:make_cli_agent_state"
        assert target.teardown == "tests.support.factories:teardown_cli_agent_env"
        assert target.harness == "stateful"
        assert target.methods == ["build_env_vars", "post_sandbox_setup"]
        assert target.include_private is True

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
