"""Fault injection primitives.

A Fault is something that can be activated (injecting failures) or deactivated
(normal behavior). The nemesis engine toggles faults during chaos testing.

Three built-in fault types:
- PatchFault: wraps a target function with fault-injecting behavior
- LambdaFault: custom activate/deactivate callables
- Subclass Fault directly for full control
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Any, Callable


class Fault(ABC):
    """Base class for all fault injections."""

    def __init__(self, name: str | None = None):
        self.name = name or self.__class__.__name__
        self.active = False

    def activate(self) -> None:
        """Activate the fault injection if not already active."""
        if not self.active:
            self._do_activate()
            self.active = True

    def deactivate(self) -> None:
        """Deactivate the fault injection if currently active."""
        if self.active:
            self._do_deactivate()
            self.active = False

    def reset(self) -> None:
        """Deactivate and clear any internal state."""
        self.deactivate()

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
            self._resolve()
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
