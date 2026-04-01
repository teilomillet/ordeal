"""Tests for ordeal's introspection catalog — using ordeal's own assertions.

Every catalog entry must be complete enough for an AI to use without
reading source. These tests enforce that contract.
"""

from __future__ import annotations

from ordeal import catalog
from ordeal.assertions import always


class TestTopLevelCatalog:
    def test_all_categories_present(self):
        c = catalog()
        expected = {
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
        }
        always(set(c.keys()) == expected, "catalog has all categories")

    def test_no_empty_categories(self):
        c = catalog()
        for cat, entries in c.items():
            always(len(entries) > 0, f"catalog[{cat}] is non-empty")


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
    def test_four_assertion_types(self):
        c = catalog()
        names = {e["name"] for e in c["assertions"]}
        always(names == {"always", "sometimes", "reachable", "unreachable"}, "all 4 assertions")

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
