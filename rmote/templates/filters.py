"""Pickleable template filter classes and the default name-to-class mapping."""

import copyreg
import inspect
import ipaddress
import json
import textwrap
from collections.abc import Callable, Iterable, Iterator, Mapping
from functools import cache
from types import MappingProxyType
from typing import Any, cast


class TemplateUndefined:
    """A missing filter result which only ``default`` may consume silently."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __str__(self) -> str:
        raise self.error

    def __bool__(self) -> bool:
        raise self.error


class TemplateFilterMeta(type):
    """Capture filter source and let pickle transfer filter classes themselves."""

    __filter_source__: str | None

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> Any:
        cls = super().__new__(mcs, name, bases, namespace)
        try:
            cls.__filter_source__ = textwrap.dedent(inspect.getsource(cls))
        except (OSError, TypeError):
            cls.__filter_source__ = None
        return cls


def reduce_template_filter(cls: TemplateFilterMeta) -> tuple[Callable[..., Any], tuple[Any, ...]]:
    """Serialize bootstrap filters by name and user filters by class source."""
    if cls.__module__ == __name__ and globals().get(cls.__name__) is cls:
        return restore_template_filter, (cls.__name__, None, ())
    source = cls.__filter_source__
    if source is None:
        raise TypeError(f"Cannot transfer filter {cls.__name__}: its class source is unavailable")
    if any(not isinstance(base, TemplateFilterMeta) for base in cls.__bases__):
        raise TypeError("Transferred filters must inherit only from TemplateFilter subclasses")
    return restore_template_filter, (cls.__name__, source, cls.__bases__)


@cache
def restore_template_filter(
    name: str, source: str | None, bases: tuple[type["TemplateFilter"], ...]
) -> type["TemplateFilter"]:
    """Rebuild a filter class without importing its original module."""
    if source is None:
        return cast(type[TemplateFilter], globals()[name])
    namespace: dict[str, Any] = {"__name__": __name__, "TemplateFilter": TemplateFilter}
    namespace.update({base.__name__: base for base in bases})
    exec(compile("from __future__ import annotations\n" + source, "<template filter>", "exec"), namespace)
    cls = namespace[name]
    if not isinstance(cls, TemplateFilterMeta) or not issubclass(cls, TemplateFilter):
        raise TypeError(f"{name} is not a TemplateFilter class")
    cls.__filter_source__ = source
    return cls


copyreg.pickle(TemplateFilterMeta, reduce_template_filter)


class TemplateFilter(metaclass=TemplateFilterMeta):
    """Base for callable filter classes transferred with a :class:`~rmote.templates.engine.Template`.

    Override ``__call__(self, value, ...)`` and pass the class in the template's
    ``filters`` mapping. Filters must be constructible without arguments; each
    render creates fresh instances. Classes and instances are pickleable,
    including classes defined inside functions. Pickle carries the class source
    and its filter base classes, so the receiver needs no user module.

    Keep filter definitions self-contained: put imports inside the class or
    method, and constants on the class. Enclosing variables, module globals and
    external decorators are not captured. Source must be in a readable file.
    Inheritance between filter classes is supported. Unpickling executes trusted
    Python code. Rendering invokes these explicitly registered filters.

    Context containers are validated without copying. Filters receive the
    original values and are responsible for any mutations they perform.

    Results are trusted values. Public attribute lookup and iterator
    consumption may execute result code; private attributes and method calls
    remain unavailable in the template language.

    Set ``handles_undefined = True`` to receive :class:`~rmote.templates.filters.TemplateUndefined` for
    missing simple lookups, as the built-in ``default`` filter does.
    """

    __filter_source__: str | None
    handles_undefined = False

    def __call__(self, value: Any, *args: Any, **kwargs: Any) -> Any:
        """Transform the piped value with the supplied filter arguments."""
        raise NotImplementedError

    @staticmethod
    def attribute(value: Any, attribute: str | int | None) -> Any:
        """Resolve a dotted path, preferring items to public object attributes."""
        if attribute is None:
            return value
        parts: list[str | int] = list(attribute.split(".")) if isinstance(attribute, str) else [attribute]
        for part in parts:
            key = int(part) if isinstance(part, str) and part.isdecimal() else part
            try:
                value = value[key]
            except (KeyError, IndexError, TypeError):
                if not isinstance(key, str):
                    raise
                if not key.isidentifier() or key.startswith("_"):
                    raise AttributeError("Private or invalid template attribute") from None
                value = getattr(value, key)
        return value


class LowerFilter(TemplateFilter):
    """Convert to a lowercase string."""

    def __call__(self, value: Any) -> Any:
        return str(value).lower()


class UpperFilter(TemplateFilter):
    """Convert to an uppercase string."""

    def __call__(self, value: Any) -> Any:
        return str(value).upper()


class CapitalizeFilter(TemplateFilter):
    """Capitalize a string."""

    def __call__(self, value: Any) -> Any:
        return str(value).capitalize()


class LengthFilter(TemplateFilter):
    """Return the length of a sized value."""

    def __call__(self, value: Any) -> Any:
        return len(value)


class ListFilter(TemplateFilter):
    """Materialize an iterable as a list."""

    def __call__(self, value: Any) -> Any:
        return list(value)


class StringFilter(TemplateFilter):
    """Convert a value to a string."""

    def __call__(self, value: Any) -> Any:
        return str(value)


class IPAddressFilter(TemplateFilter):
    """Parse an interface, optionally returning an IP operation as plain data.

    Without ``action``, return the :func:`ipaddress.ip_interface` object.
    ``action`` is ``interface``, ``address``, ``network``,
    ``network_address``, ``netmask``, ``hostmask``, ``broadcast``, ``prefix``,
    or ``version``. Prefix and version are integers; other results are strings.
    Parsing uses :func:`ipaddress.ip_interface` to retain the supplied prefix.

        >>> IPAddressFilter()("192.0.2.7/24")
        IPv4Interface('192.0.2.7/24')
        >>> IPAddressFilter()("192.0.2.7/24", "network")
        '192.0.2.0/24'
        >>> IPAddressFilter()("2001:db8::7/64", action="prefix")
        64
    """

    def __call__(
        self, value: Any, action: str | None = None
    ) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface | str | int:
        interface = ipaddress.ip_interface(value)
        match action:
            case None:
                return interface
            case "interface":
                return str(interface)
            case "address":
                return str(interface.ip)
            case "network":
                return str(interface.network)
            case "network_address":
                return str(interface.network.network_address)
            case "netmask":
                return str(interface.netmask)
            case "hostmask":
                return str(interface.hostmask)
            case "broadcast":
                return str(interface.network.broadcast_address)
            case "prefix":
                return interface.network.prefixlen
            case "version":
                return interface.version
            case _:
                raise ValueError(f"Unknown ipaddress action: {action!r}")


class TrimFilter(TemplateFilter):
    """Strip surrounding whitespace, or the supplied set of characters."""

    def __call__(self, value: Any, chars: str | None = None) -> str:
        return str(value).strip(chars)


class DefaultFilter(TemplateFilter):
    """Replace missing values, and also false values when ``boolean=True``."""

    handles_undefined = True

    def __call__(self, value: Any, default_value: Any = "", boolean: bool = False) -> Any:
        return default_value if isinstance(value, TemplateUndefined) or (boolean and not value) else value


class JoinFilter(TemplateFilter):
    """Join stringified items, optionally selecting an attribute first."""

    def __call__(self, value: Iterable[Any], d: str = "", attribute: str | int | None = None) -> str:
        return str(d).join(str(TemplateFilter.attribute(item, attribute)) for item in value)


class SortFilter(TemplateFilter):
    """Stable sort; strings compare case-insensitively unless requested otherwise."""

    def __call__(
        self,
        value: Iterable[Any],
        reverse: bool = False,
        case_sensitive: bool = False,
        attribute: str | int | None = None,
    ) -> list[Any]:
        attributes: list[str | int | None] = list(attribute.split(",")) if isinstance(attribute, str) else [attribute]

        def key(item: Any) -> list[Any]:
            values = [TemplateFilter.attribute(item, attr) for attr in attributes]
            return [v.lower() if isinstance(v, str) and not case_sensitive else v for v in values]

        return sorted(value, key=key, reverse=reverse)


class UniqueFilter(TemplateFilter):
    """Yield the first item for each distinct, hashable comparison key."""

    def __call__(
        self, value: Iterable[Any], case_sensitive: bool = False, attribute: str | int | None = None
    ) -> Iterator[Any]:
        seen = set()
        for item in value:
            key = TemplateFilter.attribute(item, attribute)
            if isinstance(key, str) and not case_sensitive:
                key = key.lower()
            if key not in seen:
                seen.add(key)
                yield item


class FirstFilter(TemplateFilter):
    """Return the first item, or an undefined result for an empty iterable."""

    def __call__(self, value: Iterable[Any]) -> Any:
        return next(iter(value), TemplateUndefined(ValueError("No first item in an empty sequence")))


class LastFilter(TemplateFilter):
    """Return the last item of a reversible sequence, or an undefined result."""

    def __call__(self, value: Any) -> Any:
        return next(reversed(value), TemplateUndefined(ValueError("No last item in an empty sequence")))


class ReverseFilter(TemplateFilter):
    """Reverse a string or return an iterator over an iterable in reverse order."""

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return value[::-1]
        try:
            return reversed(value)
        except TypeError:
            return reversed(list(value))


class IntFilter(TemplateFilter):
    """Convert to int, accepting decimal float strings and a fallback value."""

    def __call__(self, value: Any, default: int = 0, base: int = 10) -> int:
        if isinstance(value, TemplateUndefined):
            raise value.error
        try:
            return int(value, base) if isinstance(value, str) else int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError, OverflowError):
                return default


class FloatFilter(TemplateFilter):
    """Convert to float, returning the fallback on a failed conversion."""

    def __call__(self, value: Any, default: float = 0.0) -> float:
        if isinstance(value, TemplateUndefined):
            raise value.error
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


class ReplaceFilter(TemplateFilter):
    """Replace occurrences after converting the value and replacements to strings."""

    def __call__(self, value: Any, old: Any, new: Any, count: int | None = None) -> str:
        return str(value).replace(str(old), str(new), -1 if count is None else count)


class ToJSONFilter(TemplateFilter):
    """Serialize as JSON with sorted keys and escaped HTML-sensitive characters."""

    def __call__(self, value: Any, indent: int | None = None) -> str:
        result = json.dumps(value, sort_keys=True, indent=indent)
        for char in "<>&'":
            result = result.replace(char, f"\\u{ord(char):04x}")
        return result


#: Default name-to-class mapping, used only when Template receives no filters mapping.
TEMPLATE_FILTERS: Mapping[str, type[TemplateFilter]] = MappingProxyType(
    {
        "lower": LowerFilter,
        "upper": UpperFilter,
        "capitalize": CapitalizeFilter,
        "trim": TrimFilter,
        "replace": ReplaceFilter,
        "join": JoinFilter,
        "length": LengthFilter,
        "count": LengthFilter,
        "default": DefaultFilter,
        "d": DefaultFilter,
        "sort": SortFilter,
        "unique": UniqueFilter,
        "first": FirstFilter,
        "last": LastFilter,
        "reverse": ReverseFilter,
        "list": ListFilter,
        "int": IntFilter,
        "float": FloatFilter,
        "string": StringFilter,
        "tojson": ToJSONFilter,
        "ipaddress": IPAddressFilter,
    }
)


__tool_package__ = "rmote.templates"
