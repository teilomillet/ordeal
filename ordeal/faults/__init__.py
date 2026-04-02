"""Fault injection primitives.

A Fault is something that can be activated (injecting failures) or deactivated
(normal behavior). The nemesis engine toggles faults during chaos testing.

Three built-in fault types:
- PatchFault: wraps a target function with fault-injecting behavior
- LambdaFault: custom activate/deactivate callables
- Subclass Fault directly for full control

Fault modules — each function returns a ready-to-use Fault instance::

    from ordeal.faults import timing, io, numerical, network, concurrency

    faults = [
        timing.timeout("myapp.db.query"),              # raise TimeoutError
        timing.slow("myapp.db.query", delay=2.0),      # add latency
        io.error_on_call("myapp.cache.get"),            # raise IOError
        io.disk_full(),                                 # global write failure
        numerical.nan_injection("myapp.model.predict"), # inject NaN
        network.http_error("myapp.api.call", status=503),
        concurrency.contended_call("myapp.lock.acquire"),
    ]

Discover all available faults programmatically::

    from ordeal.faults import catalog
    for entry in catalog():
        print(f"{entry['module']}.{entry['name']}  -- {entry['doc']}")
"""

from __future__ import annotations

import copy
import importlib
import inspect
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable

_LOCK_TYPE = type(threading.Lock())


class Fault(ABC):
    """Base class for all fault injections.

    Thread-safe for free-threaded Python 3.13+: the ``active`` flag
    and activate/deactivate transitions are guarded by a lock.
    """

    def __init__(self, name: str | None = None):
        self.name = name or self.__class__.__name__
        self._active = False
        self._state_lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def activate(self) -> None:
        """Activate the fault injection if not already active."""
        with self._state_lock:
            if self._active:
                return
            self._do_activate()
            # PatchFault sets _skipped when the target can't be resolved.
            # Don't mark as active — the fault was never injected.
            if not getattr(self, "_skipped", False):
                self._active = True

    def deactivate(self) -> None:
        """Deactivate the fault injection if currently active."""
        with self._state_lock:
            if not self._active:
                return
            self._do_deactivate()
            self._active = False

    def reset(self) -> None:
        """Deactivate and clear any internal state."""
        self.deactivate()

    def __enter__(self) -> Fault:
        """Activate the fault as a context manager."""
        self.activate()
        return self

    def __exit__(self, *exc: object) -> None:
        """Deactivate the fault on context exit."""
        self.deactivate()

    def __deepcopy__(self, memo: dict) -> Fault:
        """Deep-copy with fresh locks (locks can't be copied/pickled)."""
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if isinstance(v, _LOCK_TYPE):
                object.__setattr__(result, k, threading.Lock())
            else:
                object.__setattr__(result, k, copy.deepcopy(v, memo))
        return result

    def __getstate__(self) -> dict:
        """Pickle support: exclude locks (recreated in __setstate__)."""
        state = self.__dict__.copy()
        lock_keys = [k for k, v in state.items() if isinstance(v, _LOCK_TYPE)]
        for k in lock_keys:
            del state[k]
        state["_lock_attrs"] = lock_keys
        return state

    def __setstate__(self, state: dict) -> None:
        """Pickle support: recreate locks."""
        lock_keys = state.pop("_lock_attrs", [])
        self.__dict__.update(state)
        for k in lock_keys:
            setattr(self, k, threading.Lock())

    @abstractmethod
    def _do_activate(self) -> None:
        """Subclasses implement this to perform the actual fault injection."""
        ...

    @abstractmethod
    def _do_deactivate(self) -> None:
        """Subclasses implement this to undo the fault injection."""
        ...

    def __repr__(self) -> str:
        return f"{self.name}({'ON' if self.active else 'OFF'})"


def _resolve_target(target: str) -> tuple[Any, str]:
    """Resolve 'package.module.attr' to (parent_object, attr_name)."""
    if "." not in target:
        raise ValueError(f"Target must be a dotted path (e.g. 'module.func'), got: {target!r}")

    parent_path, attr_name = target.rsplit(".", 1)

    # Try importing the full parent path as a module first
    try:
        parent = importlib.import_module(parent_path)
        return parent, attr_name
    except ImportError:
        pass

    # Walk from the deepest importable module through attributes
    parts = parent_path.split(".")
    obj = None
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
            for part in parts[i:]:
                obj = getattr(obj, part)
            break
        except (ImportError, AttributeError):
            continue

    if obj is None:
        raise ImportError(f"Cannot resolve target: {target!r}")

    return obj, attr_name


class PatchFault(Fault):
    """Fault that wraps a target function when active.

    Args:
        target: Dotted path to the function to patch (e.g. 'mymodule.predict').
        wrapper_fn: Receives the original function, returns a replacement.
        name: Human-readable name for this fault.
    """

    def __init__(
        self,
        target: str,
        wrapper_fn: Callable[[Callable], Callable],
        name: str | None = None,
    ):
        super().__init__(name=name or f"patch({target})")
        self.target = target
        self.wrapper_fn = wrapper_fn
        self._parent: Any = None
        self._attr_name: str | None = None
        self._original: Any = None

    def _resolve(self) -> None:
        self._parent, self._attr_name = _resolve_target(self.target)
        self._original = getattr(self._parent, self._attr_name)

    def _do_activate(self) -> None:
        if self._original is None:
            try:
                self._resolve()
            except (ImportError, AttributeError) as exc:
                import warnings

                warnings.warn(
                    f"Skipping fault {self.name!r}: cannot resolve target "
                    f"{self.target!r} ({exc}). The target may have been renamed "
                    "or removed in the installed version.",
                    stacklevel=2,
                )
                self._skipped = True
                return
        # Resolution succeeded (possibly on retry) — clear skip flag
        self._skipped = False
        wrapped = self.wrapper_fn(self._original)
        setattr(self._parent, self._attr_name, wrapped)

    def _do_deactivate(self) -> None:
        if self._parent is not None and self._attr_name is not None:
            setattr(self._parent, self._attr_name, self._original)

    def reset(self) -> None:
        super().reset()
        # Clear resolved state so re-activation re-resolves
        self._parent = None
        self._attr_name = None
        self._original = None


class LambdaFault(Fault):
    """Fault defined by activate/deactivate callables.

    Useful for one-off faults without subclassing:

        fault = LambdaFault(
            "kill-cache",
            on_activate=lambda: cache.clear(),
            on_deactivate=lambda: None,
        )
    """

    def __init__(
        self,
        name: str,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None],
    ):
        super().__init__(name=name)
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate

    def _do_activate(self) -> None:
        self._on_activate()

    def _do_deactivate(self) -> None:
        self._on_deactivate()


# ---------------------------------------------------------------------------
# Catalog — introspect available faults at runtime
# ---------------------------------------------------------------------------


def _discover_fault_modules() -> list[str]:
    """Auto-discover fault submodules by scanning the faults/ directory."""
    from pathlib import Path

    pkg_dir = Path(__file__).parent
    return sorted(p.stem for p in pkg_dir.glob("*.py") if not p.name.startswith("_"))


def catalog() -> list[dict[str, Any]]:
    """Discover all available fault factories via runtime introspection.

    Returns a list of dicts, one per fault factory function::

        [
            {
                "module": "timing",
                "name": "timeout",
                "qualname": "ordeal.faults.timing.timeout",
                "signature": "(target: str, delay: float = 30.0, ...) -> PatchFault",
                "doc": "Make *target* raise ``TimeoutError`` ...",
                "parameters": {"target": "str", "delay": "float", ...},
            },
            ...
        ]

    Fully automatic — submodules are discovered by scanning the ``faults/``
    directory.  When a new fault file or function is added, it appears here
    automatically with no registration needed.
    """
    entries: list[dict[str, Any]] = []
    for mod_name in _discover_fault_modules():
        try:
            mod = importlib.import_module(f"ordeal.faults.{mod_name}")
        except ImportError:
            continue
        for attr_name in sorted(dir(mod)):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name)
            if not callable(obj) or inspect.isclass(obj):
                continue
            try:
                sig = inspect.signature(obj)
                ret = sig.return_annotation
                # Only include functions that return a Fault subclass.
                if ret is inspect.Parameter.empty:
                    continue
                ret_name = getattr(ret, "__name__", str(ret))
                if not any(kw in ret_name for kw in ("Fault", "PatchFault")):
                    continue
            except (ValueError, TypeError):
                continue
            params = {
                p.name: getattr(p.annotation, "__name__", str(p.annotation))
                for p in sig.parameters.values()
                if p.annotation is not inspect.Parameter.empty
            }
            entries.append(
                {
                    "module": mod_name,
                    "name": attr_name,
                    "qualname": f"ordeal.faults.{mod_name}.{attr_name}",
                    "signature": str(sig),
                    "doc": (inspect.getdoc(obj) or "").split("\n")[0],
                    "parameters": params,
                }
            )
    return entries
