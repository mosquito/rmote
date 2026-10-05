"""Independent immutable snapshots with reusable frozen branches.

``freeze`` walks mutable input once. Mappings become :class:`DeepMappingProxy`,
lists/tuples become tuples, and sets become frozensets. Dataclasses become
mappings of their fields: this is a data projection, not an instance of the
original model. Unsupported objects and cycles are rejected.

Snapshots can be shared without copying. Their immutability is an API contract,
not a security boundary against Python reflection. Do not mutate input while
it is being frozen; this module does not synchronize access to source objects.
"""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any, Never, NoReturn, TypeAlias, TypeVar, cast, final, overload

__tool_package__ = "rmote.immutable"
__all__ = ["DeepMappingProxy", "FrozenValue", "freeze"]

K = TypeVar("K")
Scalar: TypeAlias = str | bytes | int | float | complex | bool | range | None
S = TypeVar("S", bound=Scalar)
FrozenValue: TypeAlias = "Scalar | DeepMappingProxy[Any] | tuple[FrozenValue, ...] | frozenset[FrozenValue]"
SCALAR_TYPES = frozenset((str, bytes, int, float, complex, bool, range, type(None)))


@final
class DeepMappingProxy(dict[K, FrozenValue]):
    """Read-only mapping owning an independent recursively frozen snapshot.

    Keys must be immutable builtin scalars or tuples/frozensets of such keys.
    Repeated references within input share one frozen value. ``copy.copy`` and
    ``copy.deepcopy`` return this object, and ``freeze(snapshot) is snapshot``.
    Pickle and ordinary module-tool transfer preserve the read-only API.

    The class is a dict subclass, so a read of a key and a copy of the whole
    mapping run at the speed of dict. Every method that would change the
    content refuses, and the type refuses an indexed assignment as well.
    Immutability stays an API contract: ``dict.__setitem__`` called explicitly
    on a snapshot still changes it, as every other reflection does.

    >>> source = {"items": [{"count": 1}]}
    >>> snapshot = DeepMappingProxy(source)
    >>> source["items"][0]["count"] = 2
    >>> snapshot["items"][0]["count"]
    1
    """

    __slots__ = ("__built",)
    __built: bool

    def __init__(self, values: Mapping[K, object]) -> None:
        # Do not allow an explicit second __init__ call to change a snapshot.
        if getattr(self, "_DeepMappingProxy__built", False):
            raise TypeError("DeepMappingProxy is immutable")
        if not isinstance(values, Mapping):
            raise TypeError("DeepMappingProxy requires a mapping")
        dict.update(self, freeze(values))
        object.__setattr__(self, "_DeepMappingProxy__built", True)

    def __repr__(self) -> str:
        return f"DeepMappingProxy({dict.__repr__(self)})"

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("DeepMappingProxy is immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("DeepMappingProxy is immutable")

    # Every method below refuses. The parameters are typed Never, so a type
    # checker refuses the call or the indexed assignment before it runs.
    def __setitem__(self, key: Never, value: Never) -> NoReturn:  # type: ignore[override]
        raise TypeError("DeepMappingProxy is immutable")

    def __delitem__(self, key: Never) -> NoReturn:  # type: ignore[override]
        raise TypeError("DeepMappingProxy is immutable")

    # A type checker asks __ior__ to agree with the __or__ of dict, which only
    # a signature that accepts the update could do. The refusal is the point
    # here, so the disagreement is expected.
    def __ior__(self, value: Never, /) -> NoReturn:  # type: ignore[override, misc]
        raise TypeError("DeepMappingProxy is immutable")

    def update(self, *args: Never, **kwargs: Never) -> NoReturn:  # type: ignore[override]
        raise TypeError("DeepMappingProxy is immutable")

    def setdefault(self, *args: Never, **kwargs: Never) -> NoReturn:  # type: ignore[override]
        raise TypeError("DeepMappingProxy is immutable")

    def pop(self, *args: Never, **kwargs: Never) -> NoReturn:  # type: ignore[override]
        raise TypeError("DeepMappingProxy is immutable")

    def popitem(self) -> NoReturn:
        raise TypeError("DeepMappingProxy is immutable")

    def clear(self) -> NoReturn:
        raise TypeError("DeepMappingProxy is immutable")

    @classmethod
    def fromkeys(cls, *args: Never, **kwargs: Never) -> NoReturn:  # type: ignore[override]
        raise TypeError("DeepMappingProxy takes a mapping, not keys")

    def copy(self) -> "DeepMappingProxy[K]":
        """Give this snapshot, because a copy of frozen data has no purpose."""
        return self

    def __copy__(self) -> "DeepMappingProxy[K]":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "DeepMappingProxy[K]":
        return self

    def __reduce__(self) -> tuple[Any, tuple[dict[K, FrozenValue]]]:
        return type(self), (dict(self),)

    def updated(self, values: Mapping[K, object]) -> "DeepMappingProxy[K]":
        """Replace complete branches in a new snapshot, sharing unchanged ones.

        Only the supplied values are frozen. The outer mapping is shallowly
        copied; nested unchanged mappings, tuples and frozensets are reused.
        The current snapshot is never changed. An empty update returns self.
        """
        if not values:
            return self
        merged = dict(self)
        merged.update(freeze(values))
        return cast(DeepMappingProxy[K], _proxy(merged))


def _proxy(values: dict[Any, FrozenValue]) -> DeepMappingProxy[Any]:
    """Build a snapshot from a freshly allocated dictionary of frozen values.

    The dictionary is copied at the speed of dict, and the frozen values
    themselves are shared, not copied again.
    """
    result = dict.__new__(DeepMappingProxy)
    dict.update(result, values)
    object.__setattr__(result, "_DeepMappingProxy__built", True)
    return result


def _immutable_key(value: object) -> bool:
    if type(value) in SCALAR_TYPES:
        return True
    if type(value) in (tuple, frozenset):
        return all(_immutable_key(child) for child in cast(Any, value))
    return False


@overload
def freeze(value: DeepMappingProxy[K]) -> DeepMappingProxy[K]: ...


@overload
def freeze(value: Mapping[K, object]) -> DeepMappingProxy[K]: ...


@overload
def freeze(value: S) -> S: ...


@overload
def freeze(value: list[Any] | tuple[object, ...]) -> tuple[FrozenValue, ...]: ...


@overload
def freeze(value: set[Any] | frozenset[Any]) -> frozenset[FrozenValue]: ...


@overload
def freeze(value: object) -> FrozenValue: ...


def freeze(value: object) -> FrozenValue:
    """Freeze a data graph once, rejecting cycles and unsupported objects.

    Already frozen mappings are reused without walking them. Ordinary tuples
    and frozensets are inspected, since they can contain mutable objects.
    Shared references are preserved within one call. Dataclass fields (including
    init=False fields) are captured without carrying class identity or methods.
    No arbitrary objects, iterators, or user-defined scalar subclasses are
    silently retained as potentially mutable leaves.
    """
    # Retain originals too: custom mappings may manufacture temporary values,
    # and their ids must not be reused during this traversal.
    memo: dict[int, tuple[object, FrozenValue]] = {}
    active: set[int] = set()

    def visit(item: Any) -> FrozenValue:
        if type(item) in SCALAR_TYPES or type(item) is DeepMappingProxy:
            return cast(FrozenValue, item)
        identity = id(item)
        if identity in active:
            raise ValueError("Cannot freeze cyclic data")
        if identity in memo:
            return memo[identity][1]
        active.add(identity)
        try:
            result: FrozenValue
            if isinstance(item, Mapping):
                data = {}
                for key, child in item.items():
                    if not _immutable_key(key):
                        raise TypeError(f"Unsupported mutable or custom mapping key: {type(key).__qualname__}")
                    data[key] = visit(child)
                result = _proxy(data)
            elif type(item) in (list, tuple):
                result = tuple(visit(child) for child in item)
            elif type(item) in (set, frozenset):
                result = frozenset(visit(child) for child in item)
            elif is_dataclass(item) and not isinstance(item, type):
                result = _proxy({field.name: visit(getattr(item, field.name)) for field in fields(item)})
            else:
                raise TypeError(f"Cannot freeze {type(item).__qualname__}")
            memo[identity] = (item, result)
            return result
        finally:
            active.remove(identity)

    return visit(value)
