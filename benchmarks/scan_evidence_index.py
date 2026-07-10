"""Benchmark repeated project-evidence discovery during a multi-target scan."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

FUNCTION_COUNT = 7
TEST_FILE_COUNT = 20
PROJECT_FILE_COUNT = 24
CALLS_PER_FUNCTION = 20


def _write_fixture(root: Path) -> None:
    functions = "\n\n".join(
        f"def fn_{index}(value: int) -> int:\n    return value + {index}\n"
        for index in range(FUNCTION_COUNT)
    )
    (root / "scanbench.py").write_text(functions, encoding="utf-8")
    calls = "\n".join(
        f"scanbench.fn_{index}({value})"
        for value in range(CALLS_PER_FUNCTION)
        for index in range(FUNCTION_COUNT)
    )
    for index in range(TEST_FILE_COUNT):
        path = root / "tests" / f"test_calls_{index}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"import scanbench\n\n{calls}\n", encoding="utf-8")
    for index in range(PROJECT_FILE_COUNT):
        (root / f"consumer_{index}.py").write_text(
            f"import scanbench\n\n{calls}\n", encoding="utf-8"
        )


def _evidence_bytes(result: object) -> bytes:
    return json.dumps(dataclasses.asdict(result), sort_keys=True, separators=(",", ":")).encode()


def run(rounds: int) -> dict[str, object]:
    from ordeal import auto as auto_mod

    with tempfile.TemporaryDirectory(prefix="ordeal-scan-index-") as temp_dir:
        root = Path(temp_dir)
        _write_fixture(root)
        previous_cwd = Path.cwd()
        sys.path.insert(0, str(root))
        os.chdir(root)
        try:
            auto_mod._candidate_seed_files_cached.cache_clear()
            auto_mod._read_python_source.cache_clear()
            auto_mod._parse_python_source.cache_clear()
            auto_mod._pytest_fixture_catalog.cache_clear()
            targets = [f"fn_{index}" for index in range(FUNCTION_COUNT)]

            index_type = auto_mod.ProjectEvidenceIndex

            def scan(*, indexed: bool) -> tuple[object, float]:
                auto_mod.ProjectEvidenceIndex = (
                    index_type if indexed else lambda _module_name: None
                )
                started = time.perf_counter()
                try:
                    result = auto_mod.scan_module(
                        "scanbench", targets=targets, max_examples=1, mode="candidate"
                    )
                finally:
                    auto_mod.ProjectEvidenceIndex = index_type
                return result, time.perf_counter() - started

            legacy_warmup, _ = scan(indexed=False)
            indexed_warmup, _ = scan(indexed=True)
            expected = _evidence_bytes(legacy_warmup)
            if _evidence_bytes(indexed_warmup) != expected:
                raise RuntimeError("indexed scan changed JSON evidence")
            legacy_times = []
            indexed_times = []
            for round_index in range(rounds):
                order = (False, True) if round_index % 2 == 0 else (True, False)
                for indexed in order:
                    result, elapsed = scan(indexed=indexed)
                    if _evidence_bytes(result) != expected:
                        raise RuntimeError("scan evidence changed between benchmark rounds")
                    (indexed_times if indexed else legacy_times).append(elapsed)
        finally:
            os.chdir(previous_cwd)
            sys.path.remove(str(root))
            sys.modules.pop("scanbench", None)
    legacy_median = statistics.median(legacy_times)
    indexed_median = statistics.median(indexed_times)
    return {
        "rounds": rounds,
        "legacy_median_seconds": legacy_median,
        "indexed_median_seconds": indexed_median,
        "latency_reduction_percent": 100.0 * (legacy_median - indexed_median) / legacy_median,
        "legacy_range_seconds": [min(legacy_times), max(legacy_times)],
        "indexed_range_seconds": [min(indexed_times), max(indexed_times)],
        "evidence_sha256": hashlib.sha256(expected).hexdigest(),
        "fixture": {
            "functions": FUNCTION_COUNT,
            "test_files": TEST_FILE_COUNT,
            "project_files": PROJECT_FILE_COUNT,
            "calls_per_function_per_file": CALLS_PER_FUNCTION,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    print(json.dumps(run(parser.parse_args().rounds), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
