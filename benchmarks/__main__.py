"""Performance benchmarks for ordeal's hot paths.

Run with:  python -m benchmarks
           python -m benchmarks snapshot    # single benchmark
           python -m benchmarks --help
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bench(name: str, fn, *, rounds: int = 50, warmup: int = 3) -> dict:
    """Run *fn* for *rounds* iterations, return timing stats."""
    for _ in range(warmup):
        fn()

    times: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)

    median = statistics.median(times)
    mean = statistics.mean(times)
    p95 = sorted(times)[int(len(times) * 0.95)]
    result = {
        "name": name,
        "rounds": rounds,
        "median_us": median * 1e6,
        "mean_us": mean * 1e6,
        "p95_us": p95 * 1e6,
        "min_us": min(times) * 1e6,
    }
    _print_row(result)
    return result


def _print_header() -> None:
    print(f"{'Benchmark':<40} {'median':>10} {'mean':>10} {'p95':>10} {'min':>10}")
    print("-" * 84)


def _print_row(r: dict) -> None:
    def _fmt(us: float) -> str:
        if us >= 1_000_000:
            return f"{us / 1_000_000:.2f}s"
        if us >= 1000:
            return f"{us / 1000:.2f}ms"
        return f"{us:.1f}us"

    print(
        f"{r['name']:<40} "
        f"{_fmt(r['median_us']):>10} "
        f"{_fmt(r['mean_us']):>10} "
        f"{_fmt(r['p95_us']):>10} "
        f"{_fmt(r['min_us']):>10}"
    )


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def bench_snapshot_restore() -> dict:
    """Measure snapshot/restore cycle for a ChaosTest machine."""
    from ordeal import ChaosTest, rule
    from ordeal.explore import Explorer
    from ordeal.faults.timing import timeout

    class _BenchTest(ChaosTest):
        faults = [timeout("time.sleep")]
        items: list = []

        @rule()
        def add(self):
            self.items.append(len(self.items))

    explorer = Explorer(_BenchTest)
    machine = _BenchTest()
    for _ in range(20):
        machine.items.append(0)

    def fn():
        snap = explorer._snapshot_machine(machine)
        explorer._restore_machine(snap)

    return _bench("snapshot_restore (20 items)", fn)


def bench_snapshot_restore_large() -> dict:
    """Snapshot/restore with larger state."""
    from ordeal import ChaosTest, rule
    from ordeal.explore import Explorer
    from ordeal.faults.timing import timeout

    class _BenchTest(ChaosTest):
        faults = [timeout("time.sleep")]
        data: dict = {}

        @rule()
        def add(self):
            pass

    explorer = Explorer(_BenchTest)
    machine = _BenchTest()
    machine.data = {f"key_{i}": list(range(50)) for i in range(100)}

    def fn():
        snap = explorer._snapshot_machine(machine)
        explorer._restore_machine(snap)

    return _bench("snapshot_restore (100x50 dict)", fn, rounds=20)


def bench_coverage_collector() -> dict:
    """Measure CoverageCollector overhead per traced function call."""
    from ordeal.explore import CoverageCollector

    collector = CoverageCollector(["benchmarks"])

    def _target_fn():
        x = 0
        for i in range(100):
            if i % 2 == 0:
                x += i
            else:
                x -= i
        return x

    def fn():
        collector.start()
        for _ in range(10):
            _target_fn()
        collector.stop()

    return _bench("coverage_collector (10x100 lines)", fn, rounds=30)


def bench_mutation_generation() -> dict:
    """Measure generate_mutants on a small function."""
    from ordeal.mutations import generate_mutants

    source = """\
def compute(a, b, mode="add"):
    if mode == "add":
        result = a + b
    elif mode == "sub":
        result = a - b
    elif mode == "mul":
        result = a * b
    else:
        result = 0
    if result < 0:
        result = -result
    return result
"""

    def fn():
        generate_mutants(source)

    return _bench("generate_mutants (12-line fn)", fn)


def bench_strategy_cache() -> dict:
    """Measure strategy_for_type with cache hits."""
    from ordeal.quickcheck import strategy_for_type

    # Prime the cache
    strategy_for_type(int)
    strategy_for_type(str)
    strategy_for_type(float)
    strategy_for_type(list[int])

    def fn():
        strategy_for_type(int)
        strategy_for_type(str)
        strategy_for_type(float)
        strategy_for_type(list[int])
        strategy_for_type(dict[str, int])

    return _bench("strategy_for_type (5 cached)", fn, rounds=200)


def bench_trace_to_dict() -> dict:
    """Measure Trace.to_dict() serialization."""
    from ordeal.trace import Trace, TraceStep

    steps = [
        TraceStep(
            kind="rule",
            name=f"rule_{i}",
            params={"x": i, "y": f"val_{i}"},
            edge_count=i * 10,
            timestamp_offset=i * 0.01,
        )
        for i in range(100)
    ]
    trace = Trace(
        run_id=1,
        seed=42,
        test_class="bench:Test",
        from_checkpoint=None,
        steps=steps,
        edges_discovered=50,
        duration=1.23,
    )

    def fn():
        trace.to_dict()

    return _bench("trace.to_dict (100 steps)", fn)


def bench_json_encoder() -> dict:
    """Measure _TraceEncoder on mixed types."""
    import json

    from ordeal.trace import _TraceEncoder

    data = {
        "normal": {"a": 1, "b": "hello", "c": [1, 2, 3]},
        "bytes_val": b"hello world",
        "set_val": {1, 2, 3, 4, 5},
        "nested": [{"x": frozenset([10, 20])}, {"y": b"\xff\x00"}],
    }

    def fn():
        json.dumps(data, cls=_TraceEncoder)

    return _bench("_TraceEncoder (mixed types)", fn, rounds=200)


# ---------------------------------------------------------------------------
# Ablation: edges-over-time for energy vs uniform vs recent
# ---------------------------------------------------------------------------


def bench_ablation() -> dict:
    """Compare checkpoint strategies on real targets.

    Runs energy, uniform, and recent on 2 targets (BranchyService,
    DeepService) and prints edge counts at 25%, 50%, 75%, and 100%
    of the run budget.  This is the "show me the ablation" benchmark.
    """
    from hypothesis.stateful import invariant, rule

    from ordeal import ChaosTest
    from ordeal.explore import Explorer
    from ordeal.faults import LambdaFault
    from tests._deep_target import DeepService
    from tests._explore_target import BranchyService

    class _BranchyChaos(ChaosTest):
        faults = [LambdaFault("noop", lambda: None, lambda: None)]

        def __init__(self):
            super().__init__()
            self.svc = BranchyService()

        @rule()
        def do_a(self):
            self.svc.step_a()

        @rule()
        def do_b(self):
            self.svc.step_b()

        @rule()
        def do_c(self):
            self.svc.step_c()

        @invariant()
        def ok(self):
            assert self.svc.state in {"init", "a", "b", "ab", "c", "deep"}

        def teardown(self):
            self.svc.reset()
            super().teardown()

    class _DeepChaos(ChaosTest):
        faults = [LambdaFault("noop", lambda: None, lambda: None)]

        def __init__(self):
            super().__init__()
            self.svc = DeepService()

        @rule()
        def do_acc(self):
            self.svc.accumulate()

        @rule()
        def do_pivot(self):
            self.svc.pivot()

        @rule()
        def do_climb(self):
            self.svc.climb()

        @rule()
        def do_strike(self):
            self.svc.strike()

        @rule()
        def do_noop(self):
            self.svc.noop()

        @invariant()
        def ok(self):
            assert self.svc.phase in {0, 1, 2, 3, 4}

        def teardown(self):
            super().teardown()

    targets = [
        ("branchy", _BranchyChaos, ["tests._explore_target"]),
        ("deep", _DeepChaos, ["tests._deep_target"]),
    ]
    strategies = ["energy", "uniform", "recent"]
    n_runs = 200
    n_seeds = 3
    quartiles = [n_runs // 4, n_runs // 2, 3 * n_runs // 4, n_runs - 1]

    print(f"\n{'':>14}", end="")
    for q in quartiles:
        print(f"  run {q + 1:>3}", end="")
    print("    final")
    print("-" * 70)

    all_results: dict = {}
    for target_name, test_cls, modules in targets:
        for strategy in strategies:
            edges_at_q: list[list[int]] = [[] for _ in quartiles]
            final_edges: list[int] = []

            for s in range(n_seeds):
                seed = 42 + s * 97
                explorer = Explorer(
                    test_cls,
                    target_modules=modules,
                    seed=seed,
                    checkpoint_strategy=strategy,
                    checkpoint_prob=0.5,
                )
                result = explorer.run(
                    max_runs=n_runs, steps_per_run=25
                )

                for i, q in enumerate(quartiles):
                    if q < len(result.edge_log):
                        edges_at_q[i].append(result.edge_log[q][1])
                    else:
                        edges_at_q[i].append(result.unique_edges)
                final_edges.append(result.unique_edges)

            label = f"{target_name}/{strategy}"
            avgs = [
                statistics.mean(eq) if eq else 0 for eq in edges_at_q
            ]
            final_avg = statistics.mean(final_edges)
            print(f"{label:>14}", end="")
            for a in avgs:
                print(f"  {a:>6.1f}", end="")
            print(f"  {final_avg:>6.1f}")

            all_results[label] = {
                "quartiles": avgs,
                "final": final_avg,
            }

    print()
    return {"name": "ablation", "results": all_results}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ALL_BENCHMARKS = {
    "snapshot": bench_snapshot_restore,
    "snapshot_large": bench_snapshot_restore_large,
    "coverage": bench_coverage_collector,
    "mutation": bench_mutation_generation,
    "strategy": bench_strategy_cache,
    "trace": bench_trace_to_dict,
    "encoder": bench_json_encoder,
    "ablation": bench_ablation,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description="ordeal performance benchmarks",
    )
    parser.add_argument(
        "names",
        nargs="*",
        choices=[*ALL_BENCHMARKS.keys(), []],
        default=[],
        help="Specific benchmarks to run (default: all)",
    )
    args = parser.parse_args()

    to_run = {k: v for k, v in ALL_BENCHMARKS.items() if not args.names or k in args.names}

    print(f"ordeal benchmarks — {len(to_run)} benchmark(s)\n")
    _print_header()

    results = []
    for name, fn in to_run.items():
        try:
            results.append(fn())
        except Exception as e:
            print(f"{name:<40} ERROR: {e}")

    print(f"\n{len(results)} benchmarks completed.")


if __name__ == "__main__":
    main()
    sys.exit(0)
