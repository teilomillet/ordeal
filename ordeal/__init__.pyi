from .auto import AutoObjectRuntime as AutoObjectRuntime
from .evidence import BugEvidenceVerification as BugEvidenceVerification
from hypothesis.stateful import Bundle as Bundle
from .migration import CandidateContract as CandidateContract
from .auto import CandidateInput as CandidateInput
from .chaos import ChaosTest as ChaosTest
from .explore import Checkpoint as Checkpoint
from .compose import ComposeCommandError as ComposeCommandError
from .compose import ComposeController as ComposeController
from .compose import ComposeExplorationResult as ComposeExplorationResult
from .compose import ComposeFailure as ComposeFailure
from .compose import ComposeRegressionArtifacts as ComposeRegressionArtifacts
from .compose import ComposeReplayReport as ComposeReplayReport
from .compose import ComposeRunner as ComposeRunner
from .compose import ComposeTrace as ComposeTrace
from .compose import ComposeTraceAction as ComposeTraceAction
from .auto import ContractCheck as ContractCheck
from .auto import ContractNotApplicable as ContractNotApplicable
from .explore import CoverageCollector as CoverageCollector
from .audit import CoverageMeasurement as CoverageMeasurement
from .audit import CoverageResult as CoverageResult
from .mine import CrossFunctionProperty as CrossFunctionProperty
from .supervisor import DeterministicSupervisor as DeterministicSupervisor
from .diff import DiffOutcome as DiffOutcome
from .diff import DiffResult as DiffResult
from .diff import DiffWitness as DiffWitness
from .diff import DivergenceWitness as DivergenceWitness
from .equivalence import EquivalenceResult as EquivalenceResult
from .evidence import EvidenceCheck as EvidenceCheck
from .explore import ExplorationResult as ExplorationResult
from .state import ExplorationState as ExplorationState
from .explore import Explorer as Explorer
from .explore import Failure as Failure
from .system_diff import FaultEvent as FaultEvent
from .audit import FunctionAudit as FunctionAudit
from .auto import FunctionResult as FunctionResult
from .state import FunctionState as FunctionState
from .auto import FuzzResult as FuzzResult
from .mutations import HardeningResult as HardeningResult
from .auto import HarnessHint as HarnessHint
from .compose import HttpResponse as HttpResponse
from .compose import HttpTransport as HttpTransport
from .system_diff import InterfaceReport as InterfaceReport
from .migration import MigrationChange as MigrationChange
from .migration import MigrationResult as MigrationResult
from .migration import MigrationStage as MigrationStage
from .mine import MineModuleResult as MineModuleResult
from .mine import MineResult as MineResult
from .mine import MinedProperty as MinedProperty
from .diff import Mismatch as Mismatch
from .audit import ModuleAudit as ModuleAudit
from .mutations import Mutant as Mutant
from .equivalence import MutantPair as MutantPair
from .scaling import MutationBenchmarkCase as MutationBenchmarkCase
from .scaling import MutationBenchmarkSuite as MutationBenchmarkSuite
from .scaling import MutationBenchmarkTrial as MutationBenchmarkTrial
from .migration import MutationGate as MutationGate
from .mutations import MutationResult as MutationResult
from .mutations import NoTestsFoundError as NoTestsFoundError
from .mutations import OPERATORS as OPERATORS
from .system_diff import Operation as Operation
from .diff import OutcomeObservation as OutcomeObservation
from .mutations import PRESETS as PRESETS
from .scaling import PerfContractCase as PerfContractCase
from .scaling import PerfContractSpec as PerfContractSpec
from .scaling import PerfContractSuite as PerfContractSuite
from .system_diff import PerformanceBudget as PerformanceBudget
from .system_diff import PerformanceResult as PerformanceResult
from .explore import ProgressSnapshot as ProgressSnapshot
from .auto import ProjectEvidenceIndex as ProjectEvidenceIndex
from .assertions import Property as Property
from .assertions import PropertyTracker as PropertyTracker
from .migration import RegressionArtifacts as RegressionArtifacts
from .metamorphic import Relation as Relation
from .metamorphic import RelationSet as RelationSet
from .assertions import ReliabilityCell as ReliabilityCell
from .chaos import RuleTimeoutError as RuleTimeoutError
from .scaling import ScalingAnalysis as ScalingAnalysis
from .auto import ScanResult as ScanResult
from .auto import SeedExample as SeedExample
from .compose import ServiceRequestError as ServiceRequestError
from .diff import SideEffect as SideEffect
from .supervisor import StateNode as StateNode
from .supervisor import StateTree as StateTree
from .audit import Status as Status
from .system_diff import StepComparison as StepComparison
from .explore import SwarmConfig as SwarmConfig
from .system_diff import SystemDiffResult as SystemDiffResult
from .system_diff import SystemEvent as SystemEvent
from .system_diff import SystemMismatch as SystemMismatch
from .audit import TestFileEvidence as TestFileEvidence
from .trace import Trace as Trace
from .trace import TraceFailure as TraceFailure
from .trace import TraceStep as TraceStep
from .supervisor import Transition as Transition
from .mutations import VerifiedTest as VerifiedTest
from .trace import ablate_faults as ablate_faults
from .buggify import activate as activate
from .assertions import always as always
from .scaling import amdahl as amdahl
from .scaling import analyze as analyze
from .audit import audit as audit
from .audit import audit_report as audit_report
from .auto import available_object_scenario_libraries as available_object_scenario_libraries
from .scaling import benchmark as benchmark
from .scaling import benchmark_perf_contract as benchmark_perf_contract
from .buggify import buggify as buggify
from .buggify import buggify_value as buggify_value
from .compose import build_compose_finding_evidence as build_compose_finding_evidence
from .auto import builtin_contract_check as builtin_contract_check
from .auto import chaos_for as chaos_for
from .chaos import chaos_test as chaos_test
from .equivalence import classify_mutant as classify_mutant
from .auto import command_arg_stability_contract as command_arg_stability_contract
from .compose import compose_reliability_coverage as compose_reliability_coverage
from .concolic import crack_branches as crack_branches
from .grammar import csv_strategy as csv_strategy
from .buggify import deactivate as deactivate
from .assertions import declare as declare
from .diff import diff as diff
from .metamorphic import discover_relations as discover_relations
from .grammar import email_strategy as email_strategy
from .concolic import enhance_mine_with_concolic as enhance_mine_with_concolic
from .cmplog import enhance_strategies as enhance_strategies
from .state import explore as explore
from .state import explore_chaos as explore_chaos
from .state import explore_harden as explore_harden
from .state import explore_mine as explore_mine
from .state import explore_mutate as explore_mutate
from .state import explore_scan as explore_scan
from .cmplog import extract_comparison_values as extract_comparison_values
from .mutagen import extract_strategy_constraint as extract_strategy_constraint
from .equivalence import filter_equivalent_mutants as filter_equivalent_mutants
from .scaling import fit_usl as fit_usl
from .auto import fuzz as fuzz
from .mutations import generate_mutants as generate_mutants
from .mutations import generate_starter_tests as generate_starter_tests
from .trace import generate_tests as generate_tests
from .auto import http_shape_contract as http_shape_contract
from .mutations import init_project as init_project
from hypothesis.stateful import initialize as initialize
from hypothesis.stateful import invariant as invariant
from .buggify import is_active as is_active
from .auto import json_roundtrip_contract as json_roundtrip_contract
from .grammar import json_strategy as json_strategy
from .auto import lifecycle_attempts_all_contract as lifecycle_attempts_all_contract
from .auto import lifecycle_followup_contract as lifecycle_followup_contract
from .auto import load_fixture_registry_modules as load_fixture_registry_modules
from .auto import load_project_fixture_registries as load_project_fixture_registries
from .compose import measure_compose_workload_strength as measure_compose_workload_strength
from .metamorphic import metamorphic as metamorphic
from .migration import migrate as migrate
from .mine import mine as mine
from .mine import mine_module as mine_module
from .mine import mine_pair as mine_pair
from .mutations import mutate as mutate
from .mutations import mutate_and_test as mutate_and_test
from .mutations import mutate_function_and_test as mutate_function_and_test
from .mutagen import mutate_inputs as mutate_inputs
from .mutagen import mutate_value as mutate_value
from .mutations import mutation_contract_context as mutation_contract_context
from .mutations import mutation_faults as mutation_faults
from .scaling import optimal_n as optimal_n
from .grammar import path_strategy as path_strategy
from .scaling import peak_throughput as peak_throughput
from hypothesis.stateful import precondition as precondition
from .auto import protected_env_keys_contract as protected_env_keys_contract
from .equivalence import prove_equivalent as prove_equivalent
from .auto import quoted_paths_contract as quoted_paths_contract
from .assertions import reachable as reachable
from .grammar import regex_strategy as regex_strategy
from .auto import register_fixture as register_fixture
from .auto import register_object_factory as register_object_factory
from .auto import register_object_harness as register_object_harness
from .auto import register_object_scenario as register_object_scenario
from .auto import register_object_setup as register_object_setup
from .auto import register_object_state_factory as register_object_state_factory
from .auto import register_object_teardown as register_object_teardown
from .trace import replay as replay
from .compose import replay_compose_trace as replay_compose_trace
from .diff import replay_diff_regression_case as replay_diff_regression_case
from .migration import replay_migration_case as replay_migration_case
from .assertions import report as report
from hypothesis.stateful import rule as rule
from .chaos import rule_timeout_context as rule_timeout_context
from .compose import run_compose_exploration as run_compose_exploration
from .compose import save_compose_regression as save_compose_regression
from .scaling import scales_linearly as scales_linearly
from .auto import scan_module as scan_module
from .buggify import set_seed as set_seed
from .auto import shell_injection_contract as shell_injection_contract
from .auto import shell_safe_contract as shell_safe_contract
from .trace import shrink as shrink
from .assertions import sometimes as sometimes
from .grammar import sql_strategy as sql_strategy
from .equivalence import statistical_equivalence as statistical_equivalence
from .equivalence import structural_equivalence as structural_equivalence
from .grammar import structured_strategy as structured_strategy
from .auto import subprocess_argv_contract as subprocess_argv_contract
from .assertions import tracker as tracker
from .assertions import unreachable as unreachable
from .grammar import url_strategy as url_strategy
from .scaling import usl as usl
from .mutations import validate_mined_properties as validate_mined_properties
from .evidence import verify_bug_evidence as verify_bug_evidence
from .audit import wilson_lower as wilson_lower
from .grammar import xml_strategy as xml_strategy

# ruff: noqa
import builtins

"""
This type stub file was generated by pyright.
"""
import re
import sys
from importlib.metadata import PackageNotFoundError, version as _get_version
from pathlib import Path
from types import ModuleType

__version__ = ...
__all__ = [
    "ChaosTest",
    "RuleTimeoutError",
    "chaos_test",
    "always",
    "declare",
    "sometimes",
    "reachable",
    "unreachable",
    "report",
    "ReliabilityCell",
    "buggify",
    "buggify_value",
    "rule",
    "invariant",
    "initialize",
    "precondition",
    "Bundle",
    "auto_configure",
    "catalog",
    "mutate",
    "mutate_function_and_test",
    "MutationResult",
    "PRESETS",
    "OPERATORS",
    "NoTestsFoundError",
    "generate_starter_tests",
    "init_project",
    "migrate",
    "MigrationResult",
    "verify_bug_evidence",
    "BugEvidenceVerification",
]
_STATEFUL_EXPORTS = ...
_LAZY_SUBMODULES = ...
_SENTINEL = ...
_CALLABLE_SUBMODULES = ...

class _CallableEntrypointModule(ModuleType):
    """A submodule that also delegates calls to its same-named entrypoint."""

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Call the function whose name matches this submodule's final component."""
        ...

class _OrdealPackage(ModuleType):
    """Prepare explicitly imported colliding submodules when accessed."""

    def __getattribute__(self, name: str) -> object:
        """Keep child modules intact while making their public entrypoints callable."""
        ...

def __getattr__(name: str) -> object:
    """Lazy import: search submodules for the requested name."""
    ...

def __dir__() -> list[str]:
    """Include lazy submodule exports in dir() for tab completion."""
    ...

def catalog() -> dict[str, list]:
    """Discover all ordeal capabilities via runtime introspection.

    Returns a dict with one key per subsystem — each value is a list of
    dicts describing the available items.  Keys: ``cli``, ``chaos``, ``faults``,
    ``invariants``, ``assertions``, ``strategies``, ``mutations``,
    ``integrations``, ``mining``, ``audit``, ``auto``, ``metamorphic``,
    ``diff``, ``migration``, ``scaling``, ``evidence``, ``exploration``, ``trace``,
    ``supervisor``,
    ``mutagen``, ``cmplog``, ``concolic``, ``grammar``, ``equivalence``.

    Everything is derived from live runtime structures — source introspection
    for Python APIs and the argparse command registry for the CLI. Adding a
    new fault, invariant, or command makes it appear here automatically.

    Each entry now includes neutral discovery metadata for models and tools:
    ``capability`` (what it does), ``applies_to`` (where it is relevant),
    ``inputs`` and ``outputs`` (expected shapes), ``examples`` (usage
    patterns), and ``learn_more`` (adjacent surfaces).

    Example::

        from ordeal import catalog
        c = catalog()
        for key in sorted(c):
            print(f"\\n{key}:")
            for item in c[key]:
                print(f"  {item['qualname']}")
                print(f"    capability: {item['capability']}")
                print(f"    applies_to: {item['applies_to']}")
                print(f"    outputs: {item['outputs']}")
    """
    ...

def auto_configure(buggify_probability: float = ..., seed: int | None = ...) -> None:
    """Enable chaos testing mode programmatically.

    Alternative to the ``--chaos`` CLI flag.  Call in ``conftest.py``::

        from ordeal import auto_configure
        auto_configure()

    Args:
        buggify_probability: Default probability for ``buggify()`` calls
            (0.0–1.0, default 0.1).
        seed: Random seed for reproducible fault scheduling.
    """
    ...
