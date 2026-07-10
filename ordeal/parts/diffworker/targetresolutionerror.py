from __future__ import annotations
# ruff: noqa
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
