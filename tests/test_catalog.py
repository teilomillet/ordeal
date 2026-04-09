"""Tests for ordeal's introspection catalog — using ordeal's own assertions.

Every catalog entry must be complete enough for an AI to use without
reading source. These tests enforce that contract.
"""

from __future__ import annotations

from ordeal import catalog
from ordeal.assertions import always
from ordeal.cli import CLI_CATALOG_SCHEMA_VERSION


class TestTopLevelCatalog:
    def test_all_categories_present(self):
        c = catalog()
        expected = {
            "cli",
            "chaos",
            "faults",
            "invariants",
            "assertions",
            "strategies",
            "mutations",
            "integrations",
            "mining",
            "audit",
            "auto",
            "metamorphic",
            "diff",
            "scaling",
            "exploration",
            "supervisor",
            "mutagen",
            "cmplog",
            "concolic",
            "grammar",
            "equivalence",
            "trace",
            "skill",
        }
        always(set(c.keys()) == expected, "catalog has all categories")

    def test_no_empty_categories(self):
        c = catalog()
        for cat, entries in c.items():
            always(len(entries) > 0, f"catalog[{cat}] is non-empty")

    def test_chaos_category_is_runtime_discoverable(self):
        c = catalog()
        names = {entry["name"] for entry in c["chaos"]}
        always(
            names == {"ChaosTest", "RuleTimeoutError", "chaos_test"},
            "chaos catalog is derived from live runtime entries",
        )

    def test_cli_category_is_runtime_discoverable(self):
        c = catalog()
        names = {entry["name"] for entry in c["cli"]}
        always("scan" in names, "cli catalog includes scan")
        always("mutate" in names, "cli catalog includes mutate")

    def test_cli_entries_include_structured_arguments(self):
        c = catalog()
        scan = next(entry for entry in c["cli"] if entry["name"] == "scan")
        always(
            scan["schema_version"] == CLI_CATALOG_SCHEMA_VERSION,
            "scan cli catalog entry exposes a stable schema version",
        )
        arg_names = {arg["name"] for arg in scan["arguments"]}
        always("target" in arg_names, "scan catalog includes target positional")
        always("seed" in arg_names, "scan catalog includes seed option")

    def test_cli_argument_metadata_is_descriptive(self):
        c = catalog()
        benchmark = next(entry for entry in c["cli"] if entry["name"] == "benchmark")
        args = benchmark["arguments"]
        mutate_target = next(a for a in args if a["name"] == "mutate_targets")
        always(mutate_target["repeatable"], "benchmark mutate target is marked repeatable")
        always(
            mutate_target["semantics"] == "repeatable",
            "benchmark mutate target records repeatable semantics",
        )

    def test_catalog_entries_include_discovery_metadata(self):
        c = catalog()
        required = {
            "subsystem",
            "subsystem_summary",
            "capability",
            "applies_to",
            "inputs",
            "outputs",
            "learn_more",
        }
        for section, entries in c.items():
            for entry in entries:
                missing = required - set(entry.keys())
                always(not missing, f"{section}/{entry['name']} exposes discovery metadata")

    def test_high_value_entries_include_examples_or_call_patterns(self):
        c = catalog()
        scan = next(entry for entry in c["cli"] if entry["name"] == "scan")
        mine = next(entry for entry in c["mining"] if entry["name"] == "mine")
        scan_module = next(entry for entry in c["auto"] if entry["name"] == "scan_module")
        always(bool(scan["examples"]), "scan command exposes example invocations")
        always(bool(mine["examples"]), "mine API exposes neutral example patterns")
        always(bool(scan_module["examples"]), "scan_module exposes neutral example patterns")


class TestFaultsCatalog:
    def test_all_modules_represented(self):
        from ordeal.faults import catalog as faults_catalog

        entries = faults_catalog()
        modules = {e["module"] for e in entries}
        expected = {"io", "timing", "numerical", "network", "concurrency"}
        always(modules == expected, "all 5 fault modules in catalog")

    def test_every_entry_has_doc(self):
        from ordeal.faults import catalog as faults_catalog

        for e in faults_catalog():
            always(len(e["doc"]) >= 20, f"fault {e['name']} has substantive doc")

    def test_every_entry_has_signature(self):
        from ordeal.faults import catalog as faults_catalog

        for e in faults_catalog():
            always(len(e["signature"]) > 0, f"fault {e['name']} has signature")

    def test_every_entry_has_qualname(self):
        from ordeal.faults import catalog as faults_catalog

        for e in faults_catalog():
            always(
                e["qualname"].startswith("ordeal.faults."),
                f"fault {e['name']} has proper qualname",
            )

    def test_count_matches_public_functions(self):
        """Catalog should find all public fault factories."""
        from ordeal.faults import catalog as faults_catalog

        entries = faults_catalog()
        # At least 20 — if this drops, a module was broken or excluded
        always(len(entries) >= 20, f"at least 20 fault factories (got {len(entries)})")

    def test_doc_contains_scenario_vocabulary(self):
        """First-line docs should contain real-world scenario keywords."""
        from ordeal.faults import catalog as faults_catalog

        scenario_words = {
            "simulates",
            "simulate",
            "inject",
            "raise",
            "truncate",
            "replace",
            "crash",
            "add",
            "provide",
            "overwrite",
            "execute",
            "run",
            "make",
        }
        for e in faults_catalog():
            doc_lower = e["doc"].lower()
            has_scenario = any(w in doc_lower for w in scenario_words)
            always(has_scenario, f"fault {e['name']} doc has action verb")


class TestInvariantsCatalog:
    def test_every_entry_has_doc(self):
        from ordeal.invariants import catalog as inv_catalog

        for e in inv_catalog():
            always(len(e["doc"]) >= 10, f"invariant {e['name']} has doc")

    def test_instances_have_real_docs(self):
        """Instance entries should have descriptive docs, not just repr()."""
        from ordeal.invariants import catalog as inv_catalog

        for e in inv_catalog():
            if e["type"] == "instance":
                always(
                    not e["doc"].startswith("Invariant("),
                    f"invariant {e['name']} has real doc, not repr()",
                )

    def test_count(self):
        from ordeal.invariants import catalog as inv_catalog

        entries = inv_catalog()
        always(len(entries) >= 10, f"at least 10 invariants (got {len(entries)})")


class TestAssertionsCatalog:
    def test_assertion_api_surface(self):
        c = catalog()
        names = {e["name"] for e in c["assertions"]}
        always(
            names == {"always", "declare", "sometimes", "reachable", "unreachable"},
            "assertion API surface",
        )

    def test_docs_indicate_behavior(self):
        """Each assertion doc should say whether it raises immediately or is deferred."""
        c = catalog()
        for e in c["assertions"]:
            doc = e["doc"].lower()
            has_behavior = "raises" in doc or "deferred" in doc
            always(has_behavior, f"assertion {e['name']} doc indicates immediate vs deferred")


class TestStrategiesCatalog:
    def test_every_entry_has_doc(self):
        c = catalog()
        for e in c["strategies"]:
            always(len(e["doc"]) >= 15, f"strategy {e['name']} has doc")

    def test_count(self):
        c = catalog()
        always(len(c["strategies"]) >= 4, "at least 4 strategies")


class TestIntegrationsCatalog:
    def test_chaos_api_test_present(self):
        c = catalog()
        names = {e["name"] for e in c["integrations"]}
        always("chaos_api_test" in names, "chaos_api_test in integrations catalog")

    def test_every_entry_has_qualname(self):
        c = catalog()
        for e in c["integrations"]:
            always(
                e["qualname"].startswith("ordeal."),
                f"integration {e['name']} has ordeal qualname",
            )
