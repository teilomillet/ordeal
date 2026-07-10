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
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints


class TargetResolutionError(RuntimeError):
    """Raised when a target cannot be imported or resolved in one revision."""


def _load_observation_layer() -> Any:
    """Load the current worker's sibling module without importing target ``ordeal``."""
    path = Path(__file__).with_name("_observation.py").resolve()
    spec = importlib_util.spec_from_file_location("_ordeal_diff_observation", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load canonical observation layer from {path}")
    module = importlib_util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_replay_codec() -> Any:
    """Load the current replay codec without importing target ``ordeal``."""
    path = Path(__file__).with_name("regression_evidence.py").resolve()
    spec = importlib_util.spec_from_file_location("_ordeal_diff_replay_codec", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load replay codec from {path}")
    module = importlib_util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_OBSERVATION = _load_observation_layer()
_REPLAY_CODEC = _load_replay_codec()
ObservationError = _OBSERVATION.ObservationError
exact_replay_match = _OBSERVATION.exact_replay_match
isolated_deepcopy = _OBSERVATION.isolated_deepcopy
observe = _OBSERVATION.observe
_decode_replay_value = _REPLAY_CODEC._decode_replay_value
_encode_replay_value = _REPLAY_CODEC._encode_replay_value


def _activate_worktree(root: Path) -> None:
    """Put the worktree's conventional import roots first on ``sys.path``."""
    for path in (root / "src", root):
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)
    os.chdir(root)


def _module_functions(module: Any, *, include_private: bool) -> dict[str, Callable[..., Any]]:
    """Return public function wrappers defined by *module*."""
    functions: dict[str, Callable[..., Any]] = {}
    for name, value in inspect.getmembers(module):
        if name.startswith("__") or (name.startswith("_") and not include_private):
            continue
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
    if not strategies:
        return [{}]

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


def _invocation_arguments(
    function: Callable[..., Any],
    values: dict[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Route generated values according to the inspected callable signature."""
    parameters = list(inspect.signature(function).parameters.values())
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    var_positional = next(
        (
            parameter
            for parameter in parameters
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL
        ),
        None,
    )
    last_positional = max(
        (index for index, parameter in enumerate(positional) if parameter.name in values),
        default=-1,
    )
    if var_positional is not None and values.get(var_positional.name):
        last_positional = len(positional) - 1

    args: list[Any] = []
    for index, parameter in enumerate(positional):
        if index > last_positional:
            break
        if parameter.name in values:
            args.append(values[parameter.name])
            continue
        if parameter.default is inspect.Parameter.empty:
            raise TypeError(
                f"generated case is missing required positional argument {parameter.name!r}"
            )
        args.append(
            isolated_deepcopy(
                parameter.default,
                label=f"default argument {parameter.name!r}",
            )
        )

    if var_positional is not None and var_positional.name in values:
        extra = values[var_positional.name]
        if not isinstance(extra, Sequence) or isinstance(extra, (str, bytes, bytearray)):
            raise TypeError(f"generated *{var_positional.name} value must be a sequence")
        args.extend(extra)

    kwargs: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.name in values:
            kwargs[parameter.name] = values[parameter.name]
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD and parameter.name in values:
            extra = values[parameter.name]
            if not isinstance(extra, Mapping):
                raise TypeError(f"generated **{parameter.name} value must be a mapping")
            overlap = kwargs.keys() & extra.keys()
            if overlap:
                names = ", ".join(sorted(str(name) for name in overlap))
                raise TypeError(f"generated keyword arguments overlap: {names}")
            kwargs.update(extra)
    return args, kwargs


def _terminal_source_location(exc: Exception, *, root: Path) -> dict[str, Any] | None:
    """Return the portable terminal traceback frame for replay identity."""
    frame = exc.__traceback__
    if frame is None:
        return None
    while frame.tb_next is not None:
        frame = frame.tb_next
    source_path = Path(frame.tb_frame.f_code.co_filename).resolve()
    try:
        path = source_path.relative_to(root).as_posix()
    except ValueError:
        path = source_path.as_posix()
    return {
        "path": path,
        "line": frame.tb_lineno,
        "function": frame.tb_frame.f_code.co_name,
    }


def _capture(
    function: Callable[..., Any],
    values: Mapping[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    """Capture one canonical outcome without exporting revision-owned objects."""
    call_values = isolated_deepcopy(
        dict(values),
        label="revision worker generated arguments",
    )
    try:
        call_args, call_kwargs = _invocation_arguments(function, call_values)
        returned = _resolve_awaitable(function(*call_args, **call_kwargs))
    except Exception as exc:
        arguments_observation = observe(
            call_values,
            label="revision worker mutated arguments",
        )
        exception = {
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "terminal_source_location": _terminal_source_location(exc, root=root),
            "canonical_exception": observe(
                exc,
                label="revision worker exception",
            ).payload,
        }
        outcome_observation = observe(
            {
                "kind": "exception",
                "exception": exception,
                "mutated_arguments": arguments_observation.payload,
            },
            label="revision worker outcome",
        )
        return {
            "kind": "exception",
            "return_value": None,
            "canonical_return_value": None,
            "exception": exception,
            "mutated_arguments": arguments_observation.json_value,
            "canonical_mutated_arguments": arguments_observation.payload,
            "canonical_observation": outcome_observation.payload,
            "observation_signature": outcome_observation.signature,
        }
    detached = isolated_deepcopy(returned, label="revision worker return value")
    return_observation = observe(detached, label="revision worker return value")
    arguments_observation = observe(
        call_values,
        label="revision worker mutated arguments",
    )
    outcome_observation = observe(
        {
            "kind": "return",
            "return_value": return_observation.payload,
            "mutated_arguments": arguments_observation.payload,
        },
        label="revision worker outcome",
    )
    return {
        "kind": "return",
        "return_value": return_observation.json_value,
        "canonical_return_value": return_observation.payload,
        "exception": None,
        "mutated_arguments": arguments_observation.json_value,
        "canonical_mutated_arguments": arguments_observation.payload,
        "canonical_observation": outcome_observation.payload,
        "observation_signature": outcome_observation.signature,
    }


def _identity(value: Any) -> Any:
    """Return one comparison value unchanged."""
    return value


def _source_binding(function: Callable[..., Any], *, root: Path | None = None) -> dict[str, Any]:
    """Bind the invoked wrapper and its declared wrapped chain to source."""
    module = str(getattr(function, "__module__", "") or "")
    qualname = str(
        getattr(function, "__qualname__", None)
        or getattr(function, "__name__", None)
        or repr(function)
    )
    source_sha256: str | None = None
    source_location: dict[str, Any] | None = None
    source_components: list[dict[str, Any]] = []
    target: Any = function
    seen: set[int] = set()
    while target is not None and id(target) not in seen:
        seen.add(id(target))
        inspection_target = getattr(target, "__code__", target)
        try:
            source = inspect.getsource(inspection_target)
            source_path = Path(
                inspect.getsourcefile(inspection_target) or inspect.getfile(inspection_target)
            ).resolve()
            _, start_line = inspect.getsourcelines(inspection_target)
        except (OSError, TypeError):
            pass
        else:
            path = source_path.as_posix()
            if root is not None:
                try:
                    path = source_path.relative_to(root).as_posix()
                except ValueError:
                    pass
            source_components.append(
                {
                    "path": path,
                    "line": start_line,
                    "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                }
            )
        target = getattr(target, "__wrapped__", None)
    if source_components:
        source_sha256 = _canonical_signature(
            [component["sha256"] for component in source_components]
        )
    inspection_function = getattr(function, "__code__", function)
    try:
        source_path = Path(
            inspect.getsourcefile(inspection_function) or inspect.getfile(inspection_function)
        ).resolve()
        _, start_line = inspect.getsourcelines(inspection_function)
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
        "source_components": source_components,
    }


def _canonical_value(value: Any) -> Any:
    """Return the shared observation layer's JSON-facing structural value."""
    return observe(value, label="revision worker value").json_value


def _canonical_json(value: Any) -> str:
    """Encode one canonical value for ordering and hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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
    return (
        observe(a, label="left tolerance value").payload
        == observe(
            b,
            label="right tolerance value",
        ).payload
    )


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
    baseline_exception = baseline["exception"]
    candidate_exception = candidate["exception"]
    if (baseline_exception is None) != (candidate_exception is None):
        return False
    if baseline_exception is not None and candidate_exception is not None:
        if any(
            baseline_exception.get(field) != candidate_exception.get(field)
            for field in ("type", "message")
        ):
            return False
    if rtol is not None or atol is not None:
        return _approx_equal(
            baseline["return_value"],
            candidate["return_value"],
            rtol=rtol if rtol is not None else 1e-9,
            atol=atol if atol is not None else 0.0,
        ) and _approx_equal(
            baseline["mutated_arguments"],
            candidate["mutated_arguments"],
            rtol=rtol if rtol is not None else 1e-9,
            atol=atol if atol is not None else 0.0,
        )
    return (
        baseline["canonical_return_value"] == candidate["canonical_return_value"]
        and baseline["canonical_mutated_arguments"] == candidate["canonical_mutated_arguments"]
    )


def _safe_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Return one detached canonical outcome envelope."""
    return {
        "kind": outcome["kind"],
        "return_value": copy.deepcopy(outcome["return_value"]),
        "canonical_return_value": copy.deepcopy(outcome["canonical_return_value"]),
        "exception": copy.deepcopy(outcome["exception"]),
        "mutated_arguments": copy.deepcopy(outcome["mutated_arguments"]),
        "canonical_mutated_arguments": copy.deepcopy(outcome["canonical_mutated_arguments"]),
        "canonical_observation": copy.deepcopy(outcome["canonical_observation"]),
        "observation_signature": str(outcome["observation_signature"]),
    }


def _canonical_signature(value: Any) -> str:
    """Return the shared observation layer's replay signature."""
    return observe(value, label="revision worker replay value").signature


def _canonical_mismatch(mismatches: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Shrink observed differences to one stable canonical runtime witness."""
    if not mismatches:
        return None
    original = mismatches[0]
    stable = [
        mismatch
        for mismatch in mismatches
        if int(mismatch["replay"]["attempts"]) > 0
        and int(mismatch["replay"]["attempts"]) == int(mismatch["replay"]["exact_matches"])
    ]
    pool = stable or list(mismatches)

    def rank(mismatch: Mapping[str, Any]) -> tuple[int, str, str]:
        encoded = _canonical_json(mismatch["args"])
        return len(encoded), encoded, str(mismatch["replay"]["expected_signature"])

    selected = copy.deepcopy(min(pool, key=rank))
    selected.update(
        {
            "original_args": copy.deepcopy(original["args"]),
            "original_canonical_args": copy.deepcopy(original["canonical_args"]),
            "original_base": copy.deepcopy(original["base"]),
            "original_candidate": copy.deepcopy(original["candidate"]),
            "minimization": {
                "method": "canonical observed-case shrinking",
                "candidate_count": len(mismatches),
                "boundary": (
                    "Selected the shortest canonical JSON input among the observed "
                    "generated divergent cases; inputs outside that sample were not explored."
                ),
            },
        }
    )
    return selected


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
        "replay_matching": (
            "exact canonical input and paired full observations, including terminal "
            "exception source locations"
        ),
    }


def _runtime() -> dict[str, Any]:
    """Return process/worktree evidence for the parent result."""
    return {"pid": os.getpid(), "worktree": str(Path.cwd().resolve())}


def _system_public_exports(system: Any) -> dict[str, str]:
    """Collect public static interface members without evaluating descriptors."""
    owner = type(system)
    exports: dict[str, str] = {}
    for name, raw in inspect.getmembers_static(owner):
        if name.startswith("_"):
            continue
        value = raw.__func__ if isinstance(raw, (classmethod, staticmethod)) else raw
        kind = "property" if isinstance(raw, property) else type(value).__name__
        try:
            signature = str(inspect.signature(value)) if callable(value) else ""
        except (TypeError, ValueError):
            signature = "<unknown>"
        exports[name] = f"{kind}{signature}"
    try:
        instance_members = vars(system)
    except TypeError:
        instance_members = {}
    for name, value in instance_members.items():
        if not name.startswith("_") and name not in exports:
            exports[name] = type(value).__name__
    return exports


def _capture_direct_call(
    call: Callable[[], Any],
    *,
    root: Path,
) -> dict[str, Any]:
    """Capture one operation or fault transition with the shared observation layer."""
    try:
        returned = _resolve_awaitable(call())
    except Exception as exc:
        exception = {
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc),
            "terminal_source_location": _terminal_source_location(exc, root=root),
            "canonical_exception": observe(
                exc,
                label="revision system exception",
            ).payload,
        }
        observation = observe(
            {"kind": "exception", "exception": exception},
            label="revision system operation outcome",
        )
        return {
            "kind": "exception",
            "return_value": None,
            "canonical_return_value": None,
            "exception": exception,
            "canonical_observation": observation.payload,
        }
    detached = isolated_deepcopy(returned, label="revision system return value")
    returned_observation = observe(detached, label="revision system return value")
    observation = observe(
        {"kind": "return", "return_value": returned_observation.payload},
        label="revision system operation outcome",
    )
    return {
        "kind": "return",
        "return_value": returned_observation.json_value,
        "canonical_return_value": returned_observation.payload,
        "exception": None,
        "canonical_observation": observation.payload,
    }


def _capture_system_sequence(
    factory: Callable[[], Any],
    sequence: Sequence[Mapping[str, Any]],
    *,
    root: Path,
) -> dict[str, Any]:
    """Run one JSON event sequence against a fresh revision-owned system."""
    inspect.signature(factory).bind()
    system = factory()
    steps: list[dict[str, Any]] = []
    for index, raw_event in enumerate(sequence):
        event = isolated_deepcopy(
            dict(raw_event),
            label="revision system event",
        )
        kind = str(event.get("kind", ""))
        if kind == "operation":
            name = str(event.get("name", ""))
            args = event.get("args", [])
            kwargs = event.get("kwargs", {})
            if not name or not isinstance(args, list) or not isinstance(kwargs, Mapping):
                raise ValueError(f"invalid operation event at index {index}")
            target = getattr(system, name)
            outcome = _capture_direct_call(
                lambda target=target, args=args, kwargs=kwargs: target(*args, **dict(kwargs)),
                root=root,
            )
        elif kind == "fault":
            name = str(event.get("name", ""))
            action = str(event.get("action", "activate"))
            parameters = event.get("parameters", {})
            if not name or not action or not isinstance(parameters, Mapping):
                raise ValueError(f"invalid fault event at index {index}")
            handler = getattr(system, "apply_fault", None)
            if not callable(handler):
                raise ValueError(
                    "revision system fault events require an apply_fault(event) method"
                )
            fault = SimpleNamespace(
                name=name,
                action=action,
                parameters=dict(parameters),
            )
            outcome = _capture_direct_call(
                lambda handler=handler, fault=fault: handler(fault), root=root
            )
        else:
            raise ValueError(f"unknown system event kind at index {index}: {kind!r}")
        try:
            public_state = {
                name: value
                for name, value in vars(system).items()
                if not name.startswith("_") and not callable(value)
            }
        except TypeError:
            public_state = {}
        state_observation = observe(public_state, label="revision system public state")
        steps.append(
            {
                "index": index,
                "event": _canonical_value(event),
                "outcome": outcome,
                "state": state_observation.json_value,
                "canonical_state": state_observation.payload,
            }
        )
    interface = _system_public_exports(system)
    sequence_observation = observe(
        {
            "interface": interface,
            "steps": [
                {
                    "event": step["event"],
                    "outcome": step["outcome"]["canonical_observation"],
                    "state": step["canonical_state"],
                }
                for step in steps
            ],
        },
        label="revision system sequence",
    )
    return {
        "kind": "system_sequence",
        "return_value": {"interface": interface, "steps": steps},
        "canonical_return_value": sequence_observation.payload,
        "exception": None,
        "mutated_arguments": {},
        "canonical_mutated_arguments": observe(
            {},
            label="revision system empty arguments",
        ).payload,
        "canonical_observation": sequence_observation.payload,
        "observation_signature": sequence_observation.signature,
    }


def _prepare_system(args: argparse.Namespace) -> None:
    """Capture one replayed system story in the baseline worktree."""
    root = Path.cwd().resolve()
    name, factory = _resolve_attribute_target(args.target, allow_missing=False)
    assert factory is not None
    sequence = json.loads(args.system_sequence)
    if not isinstance(sequence, list) or not all(isinstance(item, Mapping) for item in sequence):
        raise ValueError("system sequence must be a JSON list of event objects")
    outcome = _capture_system_sequence(factory, sequence, root=root)
    replays = [
        _capture_system_sequence(factory, sequence, root=root) for _ in range(args.replay_attempts)
    ]
    entry = {
        "signature": str(inspect.signature(factory)),
        "source": _source_binding(factory, root=root),
        "sequence": sequence,
        "outcome": outcome,
        "replays": replays,
    }
    payload = {
        "schema_version": 1,
        "target": args.target,
        "mode": "system",
        "runtime": _runtime(),
        "functions": {name: entry},
    }
    with Path(args.payload).open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    Path(args.result).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": args.target,
                "mode": "system",
                "runtime": payload["runtime"],
                "events": len(sequence),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _compare_system(args: argparse.Namespace) -> None:
    """Replay the baseline system story in the candidate worktree."""
    root = Path.cwd().resolve()
    with Path(args.payload).open("rb") as handle:
        baseline = pickle.load(handle)
    baseline_name, baseline_entry = next(iter(baseline["functions"].items()))
    try:
        _candidate_name, factory = _resolve_attribute_target(args.target, allow_missing=True)
        resolution_error = None
    except TargetResolutionError as exc:
        factory = None
        resolution_error = str(exc)
    function_results: list[dict[str, Any]] = []
    if factory is not None:
        sequence = baseline_entry["sequence"]
        baseline_outcome = baseline_entry["outcome"]
        candidate_outcome = _capture_system_sequence(factory, sequence, root=root)
        mismatch = (
            baseline_outcome["canonical_observation"] != candidate_outcome["canonical_observation"]
        )
        mismatches: list[dict[str, Any]] = []
        if mismatch:
            safe_base = _safe_outcome(baseline_outcome)
            safe_candidate = _safe_outcome(candidate_outcome)
            expected = observe(
                {"base": safe_base, "candidate": safe_candidate},
                label="revision system expected replay pair",
            )
            replay_matches = 0
            observed_signatures: list[str] = []
            for baseline_replay in baseline_entry["replays"]:
                candidate_replay = _capture_system_sequence(factory, sequence, root=root)
                observed = observe(
                    {
                        "base": _safe_outcome(baseline_replay),
                        "candidate": _safe_outcome(candidate_replay),
                    },
                    label="revision system observed replay pair",
                )
                observed_signatures.append(observed.signature)
                if exact_replay_match(
                    expected,
                    observed,
                    recorded_expected_signature=expected.signature,
                ):
                    replay_matches += 1
            mismatches.append(
                {
                    "args": {"sequence": _canonical_value(sequence)},
                    "base": safe_base,
                    "candidate": safe_candidate,
                    "replay": {
                        "attempts": len(baseline_entry["replays"]),
                        "exact_matches": replay_matches,
                        "expected_signature": expected.signature,
                        "observed_signatures": observed_signatures,
                    },
                    "minimization": {
                        "method": "supplied system sequence replay",
                        "boundary": "Git-revision system mode preserves the supplied event story.",
                    },
                }
            )
        function_results.append(
            {
                "name": baseline_name,
                "base_signature": baseline_entry["signature"],
                "candidate_signature": str(inspect.signature(factory)),
                "base_source": baseline_entry["source"],
                "candidate_source": _source_binding(factory, root=root),
                "total": len(sequence),
                "mismatch_count": int(mismatch),
                "mismatches": mismatches,
                "blocked_reason": None,
                "equivalent": not mismatch,
            }
        )
    comparison = _comparison_binding(rtol=None, atol=None)
    comparison["mode"] = "system_revision"
    result = {
        "schema_version": 1,
        "target": args.target,
        "execution_mode": "system",
        "system_sequence": baseline_entry["sequence"],
        "base_runtime": baseline["runtime"],
        "candidate_runtime": _runtime(),
        "base_mode": "system",
        "candidate_mode": "system" if factory is not None else "unresolved",
        "candidate_resolution_error": resolution_error,
        "comparison": comparison,
        "functions": function_results,
        "added_functions": [],
        "removed_functions": [baseline_name] if factory is None else [],
    }
    Path(args.result).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


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
    exact_cases = json.loads(args.exact_cases) if args.exact_cases is not None else None
    if exact_cases is not None:
        if not isinstance(exact_cases, dict):
            raise TypeError("exact revision cases must map function names to case lists")
        missing = set(exact_cases) - set(functions)
        if missing:
            raise TargetResolutionError(
                "exact revision case function(s) are missing: " + ", ".join(sorted(missing))
            )
    selected_functions = {
        name: function
        for name, function in functions.items()
        if exact_cases is None or name in exact_cases
    }
    for name, function in sorted(selected_functions.items()):
        signature = str(inspect.signature(function))
        try:
            if reason := _callable_block_reason(function):
                raise ValueError(reason)
            if exact_cases is None:
                cases = _generate_cases(
                    function,
                    root=root,
                    fixture_registries=registries,
                    max_examples=args.max_examples,
                    seed_value=args.seed,
                )
            else:
                encoded_cases = exact_cases[name]
                if not isinstance(encoded_cases, list) or not encoded_cases:
                    raise ValueError(f"exact cases for {name} must be a non-empty list")
                cases = [_decode_replay_value(case) for case in encoded_cases]
                if not all(
                    isinstance(case, dict) and all(isinstance(key, str) for key in case)
                    for case in cases
                ):
                    raise TypeError(f"exact cases for {name} must decode to string-keyed mappings")
            canonical_cases = [_canonical_value(case) for case in cases]
            canonical_case_payloads = [
                observe(case, label="revision worker input").payload for case in cases
            ]
            outcomes = [_capture(function, case, root=root) for case in cases]
            replays = [
                [_capture(function, case, root=root) for _ in range(args.replay_attempts)]
                for case in cases
            ]
            entry = {
                "signature": signature,
                "source": _source_binding(function, root=root),
                "cases": cases,
                "canonical_cases": canonical_cases,
                "canonical_case_payloads": canonical_case_payloads,
                "outcomes": outcomes,
                "replays": replays,
                "observations_canonicalized": True,
                "blocked_reason": None,
            }
        except Exception as exc:
            entry = {
                "signature": signature,
                "source": _source_binding(function, root=root),
                "cases": [],
                "canonical_cases": [],
                "canonical_case_payloads": [],
                "outcomes": [],
                "replays": [],
                "observations_canonicalized": True,
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


def _compare_cases(
    function: Callable[..., Any],
    baseline_entry: Mapping[str, Any],
    *,
    root: Path,
    rtol: float | None,
    atol: float | None,
) -> tuple[int, list[dict[str, Any]]]:
    """Compare every prepared case and retain its replay evidence privately."""
    mismatches: list[dict[str, Any]] = []
    mismatch_count = 0
    for case, canonical_case, canonical_case_payload, baseline_outcome, baseline_replays in zip(
        baseline_entry["cases"],
        baseline_entry["canonical_cases"],
        baseline_entry["canonical_case_payloads"],
        baseline_entry["outcomes"],
        baseline_entry["replays"],
        strict=True,
    ):
        if observe(case, label="candidate revision input").payload != canonical_case_payload:
            raise ObservationError(
                "candidate deserialization changed the canonical baseline input"
            )
        candidate_outcome = _capture(function, case, root=root)
        if _outcomes_equal(
            baseline_outcome,
            candidate_outcome,
            rtol=rtol,
            atol=atol,
        ):
            continue
        mismatch_count += 1
        safe_base = _safe_outcome(baseline_outcome)
        safe_candidate = _safe_outcome(candidate_outcome)
        expected_observation = observe(
            {"base": safe_base, "candidate": safe_candidate},
            label="revision worker expected replay pair",
        )
        expected_signature = expected_observation.signature
        observed_signatures: list[str] = []
        replay_matches = 0
        for baseline_replay in baseline_replays:
            candidate_replay = _capture(function, case, root=root)
            replay_pair = {
                "base": _safe_outcome(baseline_replay),
                "candidate": _safe_outcome(candidate_replay),
            }
            replay_observation = observe(
                replay_pair,
                label="revision worker observed replay pair",
            )
            observed_signatures.append(replay_observation.signature)
            if exact_replay_match(
                expected_observation,
                replay_observation,
                recorded_expected_signature=expected_signature,
            ):
                replay_matches += 1
        try:
            replay_args = _encode_replay_value(case)
        except TypeError:
            replay_args = None
        mismatches.append(
            {
                "args": canonical_case,
                "canonical_args": copy.deepcopy(canonical_case_payload),
                "replay_args": replay_args,
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
    return mismatch_count, mismatches


def _compare(args: argparse.Namespace) -> None:
    """Replay baseline cases and compare them inside the candidate worktree."""
    root = Path.cwd().resolve()
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
    if args.exact_cases is not None:
        candidate_functions = {
            name: function
            for name, function in candidate_functions.items()
            if name in baseline_functions
        }
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
            if not baseline_entry.get("observations_canonicalized"):
                blocked_reason = (
                    "baseline observations were not canonicalized before candidate import"
                )
            if blocked_reason is None:
                try:
                    mismatch_count, mismatches = _compare_cases(
                        function,
                        baseline_entry,
                        root=root,
                        rtol=args.rtol,
                        atol=args.atol,
                    )
                except ObservationError as exc:
                    blocked_reason = str(exc)
                    mismatch_count = 0
                    mismatches = []
        canonical_mismatch = _canonical_mismatch(mismatches)
        function_results.append(
            {
                "name": name,
                "base_signature": baseline_entry["signature"],
                "candidate_signature": str(inspect.signature(function)),
                "base_source": baseline_entry["source"],
                "candidate_source": _source_binding(function, root=root),
                "total": len(baseline_entry["cases"]),
                "mismatch_count": mismatch_count,
                "mismatches": [canonical_mismatch] if canonical_mismatch is not None else [],
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
    parser.add_argument("--exact-cases", default=None)
    parser.add_argument("--rtol", type=float, default=None)
    parser.add_argument("--atol", type=float, default=None)
    parser.add_argument("--replay-attempts", type=int, default=2)
    parser.add_argument("--system-sequence", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one worker phase."""
    args = _parser().parse_args(argv)
    _activate_worktree(Path.cwd().resolve())
    if args.system_sequence is not None and args.mode == "prepare":
        _prepare_system(args)
    elif args.system_sequence is not None:
        _compare_system(args)
    elif args.mode == "prepare":
        _prepare(args)
    else:
        _compare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
