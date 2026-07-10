"""Private subprocess worker for revision-isolated differential testing.

This module deliberately avoids importing :mod:`ordeal` at startup.  When the
project being compared is ordeal itself, importing the installed package would
otherwise shadow the checked-out revision in the temporary worktree.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib
import inspect
import json
import math
import os
import pickle
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, get_type_hints


class TargetResolutionError(RuntimeError):
    """Raised when a target cannot be imported or resolved in one revision."""


def _activate_worktree(root: Path) -> None:
    """Put the worktree's conventional import roots first on ``sys.path``."""
    for path in (root / "src", root):
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)
    os.chdir(root)


def _unwrap(value: Any) -> Any:
    """Unwrap common decorated-callable shapes without importing ordeal."""
    value = getattr(value, "_function", value)
    try:
        return inspect.unwrap(value)
    except (TypeError, ValueError):
        return value


def _module_functions(module: Any, *, include_private: bool) -> dict[str, Callable[..., Any]]:
    """Return public functions defined by *module*, excluding imported helpers."""
    functions: dict[str, Callable[..., Any]] = {}
    for name, value in inspect.getmembers(module):
        if name.startswith("__") or (name.startswith("_") and not include_private):
            continue
        value = _unwrap(value)
        if not (inspect.isfunction(value) or inspect.isbuiltin(value)):
            continue
        owner = getattr(value, "__module__", module.__name__)
        if owner != module.__name__:
            continue
        functions[name] = value
    return functions


def _resolve_attribute_target(
    target: str,
    *,
    allow_missing: bool,
) -> tuple[str, Callable[..., Any] | None]:
    """Resolve a dotted or colon-qualified callable target."""
    if ":" in target:
        module_name, attribute_path = target.split(":", 1)
        module = importlib.import_module(module_name)
        value: Any = module
        try:
            for part in attribute_path.split("."):
                value = getattr(value, part)
        except AttributeError:
            if allow_missing:
                return attribute_path, None
            raise TargetResolutionError(f"cannot resolve callable target: {target}") from None
        value = _unwrap(value)
        if not callable(value):
            raise TargetResolutionError(f"target is not callable: {target}")
        return attribute_path, value

    parts = target.split(".")
    imported_prefix = False
    for split_at in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split_at])
        attribute_parts = parts[split_at:]
        try:
            value = importlib.import_module(module_name)
        except ImportError:
            continue
        imported_prefix = True
        try:
            for part in attribute_parts:
                value = getattr(value, part)
        except AttributeError:
            continue
        value = _unwrap(value)
        if callable(value):
            return ".".join(attribute_parts), value
    if allow_missing and imported_prefix:
        return parts[-1], None
    raise TargetResolutionError(f"cannot resolve callable target: {target}")


def _resolve_targets(
    target: str,
    *,
    include_private: bool,
    allow_missing: bool = False,
) -> tuple[str, dict[str, Callable[..., Any]]]:
    """Resolve *target* as a module first, then as one explicit callable."""
    try:
        module = importlib.import_module(target)
    except ImportError:
        module = None
    if module is not None:
        functions = _module_functions(module, include_private=include_private)
        if not functions and not allow_missing:
            raise TargetResolutionError(f"module has no eligible functions: {target}")
        return "module", functions

    name, function = _resolve_attribute_target(target, allow_missing=allow_missing)
    if function is None:
        return "callable", {}
    return "callable", {name: function}


def _callable_block_reason(function: Callable[..., Any]) -> str | None:
    """Reject instance methods that need an object harness instead of ``self`` omission."""
    parameters = list(inspect.signature(function).parameters.values())
    if parameters and parameters[0].name == "self":
        return (
            "unbound instance method requires an object factory/harness; "
            "compare a module function or static method"
        )
    return None


def _fallback_strategies(function: Callable[..., Any]) -> dict[str, Any] | None:
    """Infer basic Hypothesis strategies when ordeal internals are unavailable."""
    import hypothesis.strategies as st

    try:
        hints = get_type_hints(function)
    except Exception:
        hints = dict(getattr(function, "__annotations__", {}))
    strategies: dict[str, Any] = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if name in {"self", "cls"}:
            continue
        if name in hints:
            try:
                strategies[name] = st.from_type(hints[name])
                continue
            except Exception:
                pass
        lowered = name.lower()
        if lowered in {"x", "y", "z", "n", "count", "index", "size", "score"}:
            strategies[name] = st.integers()
        elif any(token in lowered for token in ("name", "text", "message", "path")):
            strategies[name] = st.text()
        elif lowered.startswith(("is_", "has_", "allow_", "enable_")):
            strategies[name] = st.booleans()
        elif parameter.default is not inspect.Parameter.empty:
            continue
        else:
            return None
    return strategies


def _infer_strategies(
    function: Callable[..., Any],
    *,
    root: Path,
    fixture_registries: Sequence[str],
) -> dict[str, Any] | None:
    """Use the revision's ordeal inference when available, with a safe fallback."""
    try:
        auto = importlib.import_module("ordeal.auto")
        loader = getattr(auto, "load_project_fixture_registries", None)
        if callable(loader):
            loader(root=root, extra_modules=list(fixture_registries))
        else:
            for module_name in fixture_registries:
                importlib.import_module(module_name)
        infer = getattr(auto, "_infer_strategies")
        return infer(function)
    except Exception:
        for module_name in fixture_registries:
            importlib.import_module(module_name)
        return _fallback_strategies(function)


def _generate_cases(
    function: Callable[..., Any],
    *,
    root: Path,
    fixture_registries: Sequence[str],
    max_examples: int,
    seed_value: int,
) -> list[dict[str, Any]]:
    """Generate deterministic cases for one baseline function."""
    from hypothesis import HealthCheck, Phase, given, seed, settings

    strategies = _infer_strategies(
        function,
        root=root,
        fixture_registries=fixture_registries,
    )
    if strategies is None:
        raise ValueError(
            f"cannot infer strategies for {getattr(function, '__name__', function)!s}; "
            "add type hints or a fixture registry"
        )

    cases: list[dict[str, Any]] = []

    @seed(seed_value)
    @given(**strategies)
    @settings(
        max_examples=max_examples,
        database=None,
        deadline=None,
        phases=(Phase.generate,),
        suppress_health_check=list(HealthCheck),
    )
    def collect(**kwargs: Any) -> None:
        cases.append(kwargs)

    collect()
    return cases


def _resolve_awaitable(value: Any) -> Any:
    """Resolve async function results inside the worker process."""
    if not inspect.isawaitable(value):
        return value
    return asyncio.run(value)


def _capture(function: Callable[..., Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Capture return/exception behavior plus post-invocation arguments."""
    call_kwargs = copy.deepcopy(dict(kwargs))
    try:
        return {
            "kind": "return",
            "return_value": _resolve_awaitable(function(**call_kwargs)),
            "exception": None,
            "mutated_arguments": call_kwargs,
        }
    except Exception as exc:
        return {
            "kind": "exception",
            "return_value": None,
            "exception": {
                "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
            },
            "mutated_arguments": call_kwargs,
        }


def _identity(value: Any) -> Any:
    """Return one comparison value unchanged."""
    return value


def _source_binding(function: Callable[..., Any], *, root: Path | None = None) -> dict[str, Any]:
    """Return a callable identity bound to inspectable source text."""
    target = _unwrap(function)
    module = str(getattr(target, "__module__", "") or "")
    qualname = str(
        getattr(target, "__qualname__", None) or getattr(target, "__name__", None) or repr(target)
    )
    source_sha256: str | None = None
    source_location: dict[str, Any] | None = None
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        source = None
    if source is not None:
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    try:
        source_path = Path(inspect.getsourcefile(target) or inspect.getfile(target)).resolve()
        _, start_line = inspect.getsourcelines(target)
    except (OSError, TypeError):
        pass
    else:
        path = source_path.as_posix()
        if root is not None:
            try:
                path = source_path.relative_to(root).as_posix()
            except ValueError:
                pass
        source_location = {"path": path, "line": start_line}
    return {
        "target": f"{module}.{qualname}" if module else qualname,
        "source_sha256": source_sha256,
        "source_location": source_location,
    }


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded JSON-safe representation for reports and artifacts."""
    if depth >= 5:
        return {"type": type(value).__qualname__, "repr": repr(value)[:240]}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "float", "repr": repr(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "repr": repr(value[:120])}
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {str(key): _safe_value(item, depth=depth + 1) for key, item in value.items()}
        return {
            "type": type(value).__qualname__,
            "items": [
                [_safe_value(key, depth=depth + 1), _safe_value(item, depth=depth + 1)]
                for key, item in list(value.items())[:40]
            ],
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return {
            "type": type(value).__qualname__,
            "items": [_safe_value(item, depth=depth + 1) for item in list(value)[:80]],
        }
    if hasattr(value, "shape") and hasattr(value, "tolist"):
        try:
            return {
                "type": type(value).__qualname__,
                "shape": list(value.shape),
                "value": _safe_value(value.tolist(), depth=depth + 1),
            }
        except Exception:
            pass
    return {"type": type(value).__qualname__, "repr": repr(value)[:500]}


def _approx_equal(a: Any, b: Any, *, rtol: float, atol: float) -> bool:
    """Compare nested numeric values with tolerances."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            return True
        return abs(a - b) <= atol + rtol * abs(b)
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(
            _approx_equal(a[key], b[key], rtol=rtol, atol=atol) for key in a
        )
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            _approx_equal(left, right, rtol=rtol, atol=atol)
            for left, right in zip(a, b, strict=True)
        )
    if hasattr(a, "shape") and hasattr(b, "shape"):
        try:
            import numpy as np

            return bool(np.allclose(a, b, rtol=rtol, atol=atol))
        except (ImportError, TypeError, ValueError):
            return False
    return _exact_equal(a, b)


def _exact_equal(a: Any, b: Any) -> bool:
    """Return robust exact equality for scalar and array-like results."""
    try:
        result = a == b
        if isinstance(result, bool):
            return result
        if hasattr(result, "all"):
            return bool(result.all())
        return bool(result)
    except Exception:
        return False


def _outcomes_equal(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    rtol: float | None,
    atol: float | None,
) -> bool:
    """Compare return/exception behavior and post-invocation arguments."""
    if baseline["kind"] != candidate["kind"]:
        return False
    if baseline["exception"] != candidate["exception"]:
        return False
    compare_values: Callable[[Any, Any], bool]
    if rtol is not None or atol is not None:

        def compare_values(left: Any, right: Any) -> bool:
            return _approx_equal(
                left,
                right,
                rtol=rtol if rtol is not None else 1e-9,
                atol=atol if atol is not None else 0.0,
            )
    else:
        compare_values = _exact_equal
    return compare_values(
        baseline["return_value"],
        candidate["return_value"],
    ) and compare_values(
        baseline["mutated_arguments"],
        candidate["mutated_arguments"],
    )


def _safe_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Return one JSON-safe outcome envelope."""
    return {
        "kind": outcome["kind"],
        "return_value": _safe_value(outcome["return_value"]),
        "exception": _safe_value(outcome["exception"]),
        "mutated_arguments": _safe_value(outcome["mutated_arguments"]),
    }


def _canonical_signature(value: Any) -> str:
    """Hash one JSON-safe replay observation."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _comparison_binding(*, rtol: float | None, atol: float | None) -> dict[str, Any]:
    """Source-bind the exact revision-worker comparison pipeline."""
    comparator = _source_binding(_outcomes_equal)
    comparator.update(
        {
            "kind": "tolerance" if rtol is not None or atol is not None else "exact",
            "rtol": rtol,
            "atol": atol,
        }
    )
    normalizer = _source_binding(_identity)
    normalizer["kind"] = "identity"
    return {
        "comparator": comparator,
        "normalizer": normalizer,
        "exception_matching": "exact type and message across revisions",
        "replay_matching": "exact original input and paired full observations",
    }


def _runtime() -> dict[str, Any]:
    """Return process/worktree evidence for the parent result."""
    return {"pid": os.getpid(), "worktree": str(Path.cwd().resolve())}


def _prepare(args: argparse.Namespace) -> None:
    """Generate baseline cases and outcomes in the baseline worktree."""
    root = Path.cwd().resolve()
    mode, functions = _resolve_targets(args.target, include_private=args.include_private)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "target": args.target,
        "mode": mode,
        "runtime": _runtime(),
        "functions": {},
    }
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "target": args.target,
        "mode": mode,
        "runtime": payload["runtime"],
        "functions": [],
    }
    registries = json.loads(args.fixture_registries)
    for name, function in sorted(functions.items()):
        signature = str(inspect.signature(function))
        try:
            if reason := _callable_block_reason(function):
                raise ValueError(reason)
            cases = _generate_cases(
                function,
                root=root,
                fixture_registries=registries,
                max_examples=args.max_examples,
                seed_value=args.seed,
            )
            outcomes = [_capture(function, case) for case in cases]
            replays = [
                [_capture(function, case) for _ in range(args.replay_attempts)] for case in cases
            ]
            entry = {
                "signature": signature,
                "source": _source_binding(function, root=root),
                "cases": cases,
                "outcomes": outcomes,
                "replays": replays,
                "blocked_reason": None,
            }
        except Exception as exc:
            entry = {
                "signature": signature,
                "source": _source_binding(function, root=root),
                "cases": [],
                "outcomes": [],
                "replays": [],
                "blocked_reason": str(exc),
            }
        payload["functions"][name] = entry
        metadata["functions"].append(
            {
                "name": name,
                "signature": signature,
                "total": len(entry["cases"]),
                "blocked_reason": entry["blocked_reason"],
            }
        )

    try:
        with Path(args.payload).open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        raise RuntimeError(
            "baseline cases or outputs are not serializable across revisions: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    Path(args.result).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _compare(args: argparse.Namespace) -> None:
    """Replay baseline cases and compare them inside the candidate worktree."""
    try:
        mode, candidate_functions = _resolve_targets(
            args.target,
            include_private=args.include_private,
            allow_missing=True,
        )
        resolution_error = None
    except TargetResolutionError as exc:
        mode = "unresolved"
        candidate_functions = {}
        resolution_error = str(exc)

    try:
        with Path(args.payload).open("rb") as handle:
            baseline = pickle.load(handle)
    except Exception as exc:
        raise RuntimeError(
            f"candidate could not load baseline cases or outputs: {type(exc).__name__}: {exc}"
        ) from exc

    baseline_functions = baseline["functions"]
    baseline_names = set(baseline_functions)
    candidate_names = set(candidate_functions)
    function_results: list[dict[str, Any]] = []
    for name in sorted(baseline_names & candidate_names):
        baseline_entry = baseline_functions[name]
        function = candidate_functions[name]
        blocked_reason = baseline_entry["blocked_reason"]
        mismatches: list[dict[str, Any]] = []
        mismatch_count = 0
        if blocked_reason is None:
            for case, baseline_outcome, baseline_replays in zip(
                baseline_entry["cases"],
                baseline_entry["outcomes"],
                baseline_entry["replays"],
                strict=True,
            ):
                candidate_outcome = _capture(function, case)
                if _outcomes_equal(
                    baseline_outcome,
                    candidate_outcome,
                    rtol=args.rtol,
                    atol=args.atol,
                ):
                    continue
                mismatch_count += 1
                safe_base = _safe_outcome(baseline_outcome)
                safe_candidate = _safe_outcome(candidate_outcome)
                expected_signature = _canonical_signature(
                    {"base": safe_base, "candidate": safe_candidate}
                )
                observed_signatures: list[str] = []
                replay_matches = 0
                for baseline_replay in baseline_replays:
                    candidate_replay = _capture(function, case)
                    replay_pair = {
                        "base": _safe_outcome(baseline_replay),
                        "candidate": _safe_outcome(candidate_replay),
                    }
                    signature = _canonical_signature(replay_pair)
                    observed_signatures.append(signature)
                    if signature == expected_signature:
                        replay_matches += 1
                mismatches.append(
                    {
                        "args": _safe_value(case),
                        "base": safe_base,
                        "candidate": safe_candidate,
                        "replay": {
                            "attempts": len(baseline_replays),
                            "exact_matches": replay_matches,
                            "expected_signature": expected_signature,
                            "observed_signatures": observed_signatures,
                        },
                    }
                )
        function_results.append(
            {
                "name": name,
                "base_signature": baseline_entry["signature"],
                "candidate_signature": str(inspect.signature(function)),
                "base_source": baseline_entry["source"],
                "candidate_source": _source_binding(function, root=Path.cwd().resolve()),
                "total": len(baseline_entry["cases"]),
                "mismatch_count": mismatch_count,
                "mismatches": mismatches,
                "blocked_reason": blocked_reason,
                "equivalent": blocked_reason is None and mismatch_count == 0,
            }
        )

    result = {
        "schema_version": 1,
        "target": args.target,
        "base_runtime": baseline["runtime"],
        "candidate_runtime": _runtime(),
        "base_mode": baseline["mode"],
        "candidate_mode": mode,
        "candidate_resolution_error": resolution_error,
        "comparison": _comparison_binding(rtol=args.rtol, atol=args.atol),
        "functions": function_results,
        "added_functions": sorted(candidate_names - baseline_names),
        "removed_functions": sorted(baseline_names - candidate_names),
    }
    Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    """Build the private worker argument parser."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", choices=("prepare", "compare"))
    parser.add_argument("--target", required=True)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--include-private", action="store_true")
    parser.add_argument("--max-examples", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fixture-registries", default="[]")
    parser.add_argument("--rtol", type=float, default=None)
    parser.add_argument("--atol", type=float, default=None)
    parser.add_argument("--replay-attempts", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one worker phase."""
    args = _parser().parse_args(argv)
    _activate_worktree(Path.cwd().resolve())
    if args.mode == "prepare":
        _prepare(args)
    else:
        _compare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
