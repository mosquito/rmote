"""Small JSON hooks for dataclasses and bytes, independent of tool schemas.

The exact two-key shape ``{\"_type\": ..., \"_value\": ...}`` is reserved for
encoded values. Decode only trusted data: dataclass records import their class
and call its constructor. Ordinary JSON containers are handled by json itself.
"""

import importlib
import json
from base64 import b64decode, b64encode
from dataclasses import Field, fields, is_dataclass
from functools import lru_cache, singledispatch
from typing import Any, ClassVar, Protocol, TypeVar, cast, overload

__tool_package__ = "rmote.serialization"
__all__ = ["Dataclass", "json_default", "json_object_hook", "dump_dataclass", "load_dataclass"]


class Dataclass(Protocol):
    """Structural type for a dataclass instance, independent of collectors."""

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]


T = TypeVar("T", bound=Dataclass)


@lru_cache(maxsize=512)
def resolve_class(path: str) -> type[Dataclass]:
    """Resolve an importable dataclass, rejecting local classes and other types.

    The answer depends only on the path, and a document can repeat one path
    thousands of times, so it is cached. A rejected path raises instead of
    filling the cache.
    """
    try:
        module, separator, name = path.partition(":")
        if not separator or not name or "<locals>" in name:
            raise ValueError("Expected an importable module:qualname")
        value = importlib.import_module(module)
        for part in name.split("."):
            value = getattr(value, part)
        if not isinstance(value, type) or not is_dataclass(value):
            raise ValueError(f"Not a dataclass: {path}")
        return value
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"Cannot resolve dataclass: {path}") from exc


@lru_cache(maxsize=512)
def dataclass_path(cls: type) -> str:
    """Return the importable path of *cls*, checking its identity once.

    The path depends only on the class object, and a class cannot turn into a
    different one. A reloaded module defines a new class, which asks again
    under its own key.
    """
    path = f"{cls.__module__}:{cls.__qualname__}"
    if resolve_class(path) is not cls:
        raise ValueError(f"Dataclass identity changed: {path}")
    return path


@lru_cache(maxsize=512)
def init_fields(cls: type) -> tuple[str, ...]:
    """Return the names of the constructor fields of a dataclass."""
    return tuple(field.name for field in fields(cls) if field.init)


@singledispatch
def json_default(value: object) -> dict[str, Any]:
    """JSON default hook for dataclasses, extensible through singledispatch.

    Values already supported by json do not pass through this hook. For a
    dataclass it emits a class path and shallow constructor-field mapping;
    json recursively visits their values. No asdict or deepcopy is performed.
    Unknown objects raise TypeError. init=False fields are recomputed on load.
    """
    if is_dataclass(value) and not isinstance(value, type):
        cls = type(value)
        return {
            "_type": "dataclass",
            "_value": {
                "class": dataclass_path(cls),
                "fields": {name: getattr(value, name) for name in init_fields(cls)},
            },
        }
    raise TypeError(f"Object of type {type(value).__qualname__} is not JSON serializable")


@json_default.register
def bytes_default(value: bytes) -> dict[str, str]:
    """Encode binary data as a tagged base64 string."""
    return {"_type": "bytes", "_value": b64encode(value).decode("ascii")}


def json_object_hook(value: dict[str, Any]) -> Any:
    """Decode reserved tagged records; leave ordinary dictionaries unchanged.

    Use as json.loads(object_hook=...). Children have already been decoded.
    Unknown or malformed reserved records are errors rather than cache misses.
    """
    if set(value) != {"_type", "_value"}:
        return value
    match value:
        case {"_type": "bytes", "_value": str(payload)}:
            return b64decode(payload, validate=True)
        case {"_type": "dataclass", "_value": {"class": str(path), "fields": dict(values)} as model}:
            if set(model) != {"class", "fields"}:
                raise ValueError("Invalid dataclass record")
            cls = resolve_class(path)
            try:
                return cls(**values)
            except TypeError as exc:
                raise ValueError(f"Invalid fields for {path}") from exc
        case _:
            raise ValueError("Unknown or invalid tagged JSON value")


def dump_dataclass(value: Dataclass) -> dict[str, Any]:
    """Export an entire dataclass tree to JSON-compatible tagged dictionaries.

    This convenience helper materializes a tree through JSON. Backends use
    json_default directly instead, avoiding this intermediate representation.
    """
    if not is_dataclass(value) or isinstance(value, type):
        raise ValueError("Expected a dataclass instance")
    return cast(dict[str, Any], json.loads(json.dumps(value, default=json_default, allow_nan=False)))


@overload
def load_dataclass(value: dict[str, Any], expected: type[T]) -> T: ...


@overload
def load_dataclass(value: dict[str, Any], expected: None = None) -> Dataclass: ...


def load_dataclass(value: dict[str, Any], expected: type[T] | None = None) -> Dataclass:
    """Restore a dataclass from tagged dictionaries; optionally check its type."""
    result = json.loads(json.dumps(value, allow_nan=False), object_hook=json_object_hook)
    if not is_dataclass(result) or isinstance(result, type):
        raise ValueError("Expected a dataclass record")
    if expected is not None and not isinstance(result, expected):
        raise ValueError(f"Expected {expected.__qualname__}")
    return result
