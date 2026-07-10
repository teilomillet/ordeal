from __future__ import annotations
# ruff: noqa
import copy
import hashlib
import inspect
import json
import math
import struct
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath
from types import MappingProxyType, ModuleType
from typing import Any
_SCHEMA = "ordeal.canonical-observation/v1"
class ObservationError(RuntimeError):
    """Signal that a value cannot be isolated or represented losslessly."""
@dataclass(frozen=True)
class CanonicalObservation:
    """One typed structural graph and its deterministic replay signature."""

    payload: dict[str, Any]
    signature: str
    public_value: Any
    json_value: Any
    _mutable_ids: frozenset[int] = field(repr=False, compare=False)
def _type_name(value: Any) -> str:
    """Return a module-qualified runtime type without calling the value's repr."""
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"
def _hash_payload(payload: Mapping[str, Any]) -> str:
    """Hash one canonical JSON payload."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
def _float_bits(value: float) -> str:
    """Return every IEEE-754 bit, including signed zero and NaN payloads."""
    return struct.pack(">d", value).hex()
def _slot_names(cls: type[Any]) -> tuple[tuple[str, str], ...]:
    """Return declared storage names and stable labels across one class MRO."""
    names: list[tuple[str, str]] = []
    for owner in reversed(cls.__mro__):
        declared = owner.__dict__.get("__slots__", ())
        if isinstance(declared, str):
            declared = (declared,)
        for name in declared:
            if name in {"__dict__", "__weakref__"}:
                continue
            actual = name
            if name.startswith("__") and not name.endswith("__"):
                actual = f"_{owner.__name__.lstrip('_')}{name}"
            label = f"{owner.__module__}.{owner.__qualname__}:{name}"
            names.append((actual, label))
    return tuple(names)
class _SnapshotBuilder:
    """Encode one Python value as a typed object graph without equality or repr."""

    def __init__(self, *, label: str) -> None:
        self.label = label
        self.memo: dict[int, int] = {}
        self.nodes: list[dict[str, Any]] = []
        self.mutable_ids: set[int] = set()

    def build(self, value: Any) -> dict[str, Any]:
        """Return the complete canonical graph payload."""
        try:
            root = self._encode(value)
        except ObservationError:
            raise
        except Exception as exc:
            raise ObservationError(
                f"could not represent {self.label} losslessly: {type(exc).__name__}: {exc}"
            ) from exc
        return {"schema": _SCHEMA, "root": root, "nodes": self.nodes}

    def _atom(self, value: Any) -> dict[str, Any] | None:
        """Encode exact immutable scalar types, or return ``None`` for a node."""
        value_type = type(value)
        if value is None:
            return {"kind": "none"}
        if value_type is bool:
            return {"kind": "bool", "value": value}
        if value_type is int:
            return {"kind": "int", "value": str(value)}
        if value_type is str:
            return {"kind": "str", "value": value}
        if value_type is float:
            return {"kind": "float", "bits": _float_bits(value)}
        if value_type is complex:
            return {
                "kind": "complex",
                "real_bits": _float_bits(value.real),
                "imag_bits": _float_bits(value.imag),
            }
        if value_type is bytes:
            return {"kind": "bytes", "hex": value.hex()}
        if value is Ellipsis:
            return {"kind": "ellipsis"}
        if value is NotImplemented:
            return {"kind": "not_implemented"}
        return None

    def _new_node(self, value: Any, *, mutable: bool) -> tuple[int, dict[str, Any]]:
        """Reserve a node before descending so aliases and cycles remain visible."""
        identity = id(value)
        index = len(self.nodes)
        self.memo[identity] = index
        node: dict[str, Any] = {"id": index}
        self.nodes.append(node)
        if mutable:
            self.mutable_ids.add(identity)
        return index, node

    def _encode(self, value: Any) -> dict[str, Any]:
        """Encode one value or reference into the current graph."""
        atom = self._atom(value)
        if atom is not None:
            return atom

        identity = id(value)
        if identity in self.memo:
            return {"kind": "ref", "id": self.memo[identity]}

        if isinstance(value, Enum):
            index, node = self._new_node(value, mutable=False)
            node.update(
                {
                    "kind": "enum",
                    "type": _type_name(value),
                    "name": value.name,
                    "value": self._encode(value.value),
                }
            )
            return {"kind": "ref", "id": index}

        if isinstance(value, PurePath):
            index, node = self._new_node(value, mutable=False)
            node.update(
                {
                    "kind": "path",
                    "type": _type_name(value),
                    "parts": [self._encode(part) for part in value.parts],
                }
            )
            return {"kind": "ref", "id": index}

        if type(value) is range:
            return {
                "kind": "range",
                "start": str(value.start),
                "stop": str(value.stop),
                "step": str(value.step),
            }
        if type(value) is slice:
            index, node = self._new_node(value, mutable=False)
            node.update(
                {
                    "kind": "slice",
                    "start": self._encode(value.start),
                    "stop": self._encode(value.stop),
                    "step": self._encode(value.step),
                }
            )
            return {"kind": "ref", "id": index}

        if isinstance(value, tuple) and hasattr(type(value), "_fields"):
            fields = tuple(getattr(type(value), "_fields"))
            if not all(type(name) is str for name in fields):
                raise ObservationError(
                    f"could not represent {self.label} losslessly: invalid namedtuple fields"
                )
            index, node = self._new_node(value, mutable=False)
            node.update(
                {
                    "kind": "namedtuple",
                    "type": _type_name(value),
                    "fields": [
                        [name, self._encode(tuple.__getitem__(value, offset))]
                        for offset, name in enumerate(fields)
                    ],
                }
            )
            return {"kind": "ref", "id": index}

        if isinstance(value, list):
            index, node = self._new_node(value, mutable=True)
            node.update(
                {
                    "kind": "list",
                    "type": _type_name(value),
                    "items": [self._encode(item) for item in list.__iter__(value)],
                }
            )
            self._add_object_state(value, node, required=type(value) is not list)
            return {"kind": "ref", "id": index}

        if isinstance(value, tuple):
            index, node = self._new_node(value, mutable=False)
            node.update(
                {
                    "kind": "tuple",
                    "type": _type_name(value),
                    "items": [self._encode(item) for item in tuple.__iter__(value)],
                }
            )
            self._add_object_state(value, node, required=type(value) is not tuple)
            return {"kind": "ref", "id": index}

        if isinstance(value, OrderedDict):
            index, node = self._new_node(value, mutable=True)
            node.update(
                {
                    "kind": "ordered_dict",
                    "type": _type_name(value),
                    "items": [
                        [self._encode(key), self._encode(item)]
                        for key, item in OrderedDict.items(value)
                    ],
                }
            )
            self._add_object_state(value, node, required=False)
            return {"kind": "ref", "id": index}

        if isinstance(value, dict):
            index, node = self._new_node(value, mutable=True)
            node.update(
                {
                    "kind": "dict",
                    "type": _type_name(value),
                    "items": [
                        [self._encode(key), self._encode(item)] for key, item in dict.items(value)
                    ],
                }
            )
            self._add_object_state(value, node, required=type(value) is not dict)
            return {"kind": "ref", "id": index}

        if isinstance(value, (set, frozenset)):
            mutable = isinstance(value, set)
            index, node = self._new_node(value, mutable=mutable)
            encoded_items = [self._standalone_set_item(item) for item in value]
            encoded_items.sort(
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
            )
            node.update(
                {
                    "kind": "set" if mutable else "frozenset",
                    "type": _type_name(value),
                    "items": encoded_items,
                }
            )
            self._add_object_state(
                value,
                node,
                required=type(value) not in {set, frozenset},
            )
            return {"kind": "ref", "id": index}

        if type(value) is bytearray:
            index, node = self._new_node(value, mutable=True)
            node.update({"kind": "bytearray", "hex": bytes(value).hex()})
            return {"kind": "ref", "id": index}

        if type(value) is memoryview:
            index, node = self._new_node(value, mutable=not value.readonly)
            if value.format == "O":
                raise ObservationError(
                    f"could not represent {self.label} losslessly: object memoryview"
                )
            node.update(
                {
                    "kind": "memoryview",
                    "format": value.format,
                    "itemsize": value.itemsize,
                    "ndim": value.ndim,
                    "shape": list(value.shape) if value.shape is not None else None,
                    "strides": list(value.strides) if value.strides is not None else None,
                    "readonly": value.readonly,
                    "hex": value.tobytes().hex(),
                }
            )
            return {"kind": "ref", "id": index}

        array_node = self._encode_array(value)
        if array_node is not None:
            return array_node

        if isinstance(value, (ModuleType, type)) or inspect.isroutine(value):
            raise ObservationError(
                f"could not represent {self.label} losslessly: {_type_name(value)} "
                "has executable identity but no structural value representation"
            )

        if isinstance(value, MappingProxyType):
            index, node = self._new_node(value, mutable=False)
            node.update(
                {
                    "kind": "mapping_proxy",
                    "items": [
                        [self._encode(key), self._encode(item)] for key, item in value.items()
                    ],
                }
            )
            return {"kind": "ref", "id": index}

        if isinstance(value, Mapping):
            index, node = self._new_node(value, mutable=True)
            node.update(
                {
                    "kind": "mapping",
                    "type": _type_name(value),
                    "items": [
                        [self._encode(key), self._encode(item)] for key, item in value.items()
                    ],
                }
            )
            self._add_object_state(value, node, required=False)
            return {"kind": "ref", "id": index}

        index, node = self._new_node(value, mutable=True)
        node.update({"kind": "exception" if isinstance(value, BaseException) else "object"})
        node["type"] = _type_name(value)
        if isinstance(value, BaseException):
            node["args"] = self._encode(value.args)
            node["suppress_context"] = value.__suppress_context__
            node["cause"] = self._encode(value.__cause__)
            node["context"] = self._encode(value.__context__)
        self._add_object_state(value, node, required=True)
        return {"kind": "ref", "id": index}

    def _encode_array(self, value: Any) -> dict[str, Any] | None:
        """Encode NumPy-compatible dense arrays without converting through repr."""
        if not (
            hasattr(value, "dtype")
            and hasattr(value, "shape")
            and hasattr(value, "tobytes")
            and type(value).__module__.split(".", 1)[0] == "numpy"
        ):
            return None
        dtype = value.dtype
        if bool(getattr(dtype, "hasobject", False)):
            raise ObservationError(
                f"could not represent {self.label} losslessly: object-dtype array"
            )
        index, node = self._new_node(value, mutable=True)
        try:
            descriptor = dtype.descr if dtype.fields is not None else dtype.str
            raw = value.tobytes(order="A")
            strides = list(value.strides)
            shape = list(value.shape)
        except Exception as exc:
            raise ObservationError(
                f"could not represent {self.label} losslessly: array extraction failed: {exc}"
            ) from exc
        node.update(
            {
                "kind": "ndarray",
                "type": _type_name(value),
                "dtype": descriptor,
                "shape": shape,
                "strides": strides,
                "writeable": bool(getattr(value.flags, "writeable", True)),
                "hex": raw.hex(),
            }
        )
        self._add_object_state(value, node, required=False)
        return {"kind": "ref", "id": index}

    def _standalone_set_item(self, value: Any) -> dict[str, Any]:
        """Encode an order-independent set item without cross-item aliases."""
        builder = _SnapshotBuilder(label=f"set item in {self.label}")
        payload = builder.build(value)
        if builder.mutable_ids:
            raise ObservationError(
                f"could not represent {self.label} losslessly: set contains mutable state"
            )
        return payload

    def _add_object_state(
        self,
        value: Any,
        node: dict[str, Any],
        *,
        required: bool,
    ) -> None:
        """Append instance dictionary and declared slots without evaluating properties."""
        state_found = False
        try:
            namespace = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            namespace = None
        if namespace is not None:
            if not isinstance(namespace, dict):
                raise ObservationError(
                    f"could not represent {self.label} losslessly: non-dict instance namespace"
                )
            node["attributes"] = [
                [self._encode(key), self._encode(item)] for key, item in namespace.items()
            ]
            state_found = True

        slots: list[list[Any]] = []
        for actual, label in _slot_names(type(value)):
            try:
                item = object.__getattribute__(value, actual)
            except AttributeError:
                slots.append([label, False, None])
            except Exception as exc:
                raise ObservationError(
                    f"could not represent {self.label} losslessly: slot {label!r} failed: {exc}"
                ) from exc
            else:
                slots.append([label, True, self._encode(item)])
            state_found = True
        if slots:
            node["slots"] = slots
        if required and not state_found and not isinstance(value, BaseException):
            raise ObservationError(
                f"could not represent {self.label} losslessly: opaque {_type_name(value)}"
            )
def _friendly_value(value: Any, payload: dict[str, Any], *, json_safe: bool) -> Any:
    """Return a non-repr convenience view, falling back to the structural graph."""
    seen: set[int] = set()

    def convert(item: Any) -> Any:
        item_type = type(item)
        if item is None or item_type in {bool, int, str}:
            return item
        if item_type is float:
            if json_safe and not math.isfinite(item):
                raise ValueError
            return item
        if item_type is bytes:
            if json_safe:
                return {"type": "bytes", "hex": item.hex()}
            return item
        identity = id(item)
        if identity in seen:
            raise ValueError
        if isinstance(item, list):
            seen.add(identity)
            try:
                return [convert(child) for child in list.__iter__(item)]
            finally:
                seen.remove(identity)
        if isinstance(item, tuple):
            seen.add(identity)
            try:
                converted = [convert(child) for child in tuple.__iter__(item)]
            finally:
                seen.remove(identity)
            return converted if json_safe else tuple(converted)
        if isinstance(item, OrderedDict):
            pairs = list(OrderedDict.items(item))
            if not all(type(key) is str for key, _value in pairs):
                raise ValueError
            seen.add(identity)
            try:
                return {key: convert(child) for key, child in pairs}
            finally:
                seen.remove(identity)
        if isinstance(item, dict) and all(type(key) is str for key in dict.keys(item)):
            seen.add(identity)
            try:
                return {key: convert(child) for key, child in dict.items(item)}
            finally:
                seen.remove(identity)
        try:
            namespace = object.__getattribute__(item, "__dict__")
        except (AttributeError, TypeError):
            namespace = None
        if isinstance(namespace, dict) and all(type(key) is str for key in namespace):
            seen.add(identity)
            try:
                state = {key: convert(child) for key, child in namespace.items()}
            finally:
                seen.remove(identity)
            return {"type": _type_name(item), "state": state}
        raise ValueError

    try:
        return convert(value)
    except (TypeError, ValueError):
        return payload
def observe(value: Any, *, label: str = "observation") -> CanonicalObservation:
    """Structurally snapshot one value or raise when no lossless encoding exists."""
    builder = _SnapshotBuilder(label=label)
    payload = builder.build(value)
    return CanonicalObservation(
        payload=payload,
        signature=_hash_payload(payload),
        public_value=_friendly_value(value, payload, json_safe=False),
        json_value=_friendly_value(value, payload, json_safe=True),
        _mutable_ids=frozenset(builder.mutable_ids),
    )
