"""Shared fact models and collector validation; no cache or transport I/O."""

from collections.abc import Coroutine, Iterable
from dataclasses import is_dataclass
from typing import Any, Protocol, TypedDict, TypeVar, get_type_hints

from rmote.serialization import Dataclass

from .collectors import (
    AptFacts,
    CpuFacts,
    MemoryFacts,
    NetworkdFacts,
    NetworkFacts,
    PacmanFacts,
    PythonFacts,
    StorageFacts,
    SystemdFacts,
    SystemdResolvedFacts,
    SystemdTimesyncFacts,
    SystemFacts,
)
from .collectors.apt import AptInfo
from .collectors.cpu import CpuInfo
from .collectors.memory import MemoryInfo
from .collectors.network import NetworkdInfo, NetworkInfo
from .collectors.pacman import PacmanInfo
from .collectors.python import PythonInfo
from .collectors.storage import StorageInfo
from .collectors.system import SystemInfo
from .collectors.systemd import ResolvedInfo, SystemdInfo, TimesyncInfo

T_co = TypeVar("T_co", bound=Dataclass, covariant=True)


class Collector(Protocol[T_co]):
    """Structural interface implemented by a Tool class owning one facts key.

    Increment ``version`` whenever the cached shape or meaning changes.
    ``collect`` runs on the target and returns the complete branch, not a patch.
    """

    key: str
    version: int

    @property
    def result_type(self) -> type[T_co]: ...

    @staticmethod
    def collect() -> T_co | Coroutine[Any, Any, T_co]: ...


class FactsData(TypedDict, total=False):
    """Optional built-in fact branches, stored in an ordinary dictionary.

    Subsets and offline reads can omit any key. Extend this schema with the
    keys of custom collectors; their values remain dataclass instances.
    """

    apt: AptInfo
    pacman: PacmanInfo
    system: SystemInfo
    cpu: CpuInfo
    memory: MemoryInfo
    python: PythonInfo
    network: NetworkInfo
    networkd: NetworkdInfo
    storage: StorageInfo
    systemd: SystemdInfo
    systemd_timesync: TimesyncInfo
    systemd_resolved: ResolvedInfo


#: Built-in collector classes used when gather, fetch or validate omits collectors.
#: Pass a different iterable to those helpers to replace this set for that call.
DEFAULT_COLLECTORS: tuple[Collector[Any], ...] = (
    AptFacts,
    CpuFacts,
    MemoryFacts,
    PacmanFacts,
    SystemFacts,
    PythonFacts,
    NetworkFacts,
    NetworkdFacts,
    StorageFacts,
    SystemdFacts,
    SystemdTimesyncFacts,
    SystemdResolvedFacts,
)


class CollectorRegistry:
    """Validate collector definitions and select independent fact branches.

    ``fetch``, ``gather`` and ``validate`` construct a registry for each call.
    Custom collection code can also use one directly. It performs no collection,
    transport or cache I/O. The ``collectors`` attribute maps keys to the supplied
    collector classes in registration order.

    Args:
        collectors: Collector classes to register, replacing DEFAULT_COLLECTORS.
            Each needs a unique nonempty string key, a positive integer version,
            a callable collect method and a dataclass result_type. A built-in key
            must retain its FactsData model type or a subclass of that model.

    Raises:
        ValueError: A collector definition violates these requirements.

    Example using a pre-existing result without collecting or opening a connection::

        >>> registry = CollectorRegistry((PythonFacts,))
        >>> registry.select()
        ('python',)
        >>> registry.select(['python', 'python'])
        ('python',)
        >>> registry.validate('python', PythonInfo(version='3.11', executable='/usr/bin/python3', implementation='CPython'))
    """

    def __init__(self, collectors: Iterable[Collector[Any]] = DEFAULT_COLLECTORS) -> None:
        self.collectors: dict[str, Collector[Any]] = {}
        builtin_models = get_type_hints(FactsData)
        for collector in collectors:
            if not isinstance(collector.key, str) or not collector.key:
                raise ValueError("Collector key must be a nonempty string")
            if collector.key in self.collectors:
                raise ValueError(f"Duplicate collector key: {collector.key}")
            if type(collector.version) is not int or collector.version < 1:
                raise ValueError("Collector version must be a positive integer")
            if not callable(collector.collect):
                raise ValueError("Collector collect must be callable")
            if not isinstance(collector.result_type, type) or not is_dataclass(collector.result_type):
                raise ValueError("Collector result_type must be a dataclass")
            expected = builtin_models.get(collector.key)
            if expected is not None and not issubclass(collector.result_type, expected):
                raise ValueError(f"Collector {collector.key} must declare a {expected.__qualname__} result_type")
            self.collectors[collector.key] = collector

    def select(self, sections: Iterable[str] | None = None) -> tuple[str, ...]:
        """Return unique selected keys in order, or all keys when sections is None.

        An empty iterable selects nothing. A string is not a list of keys.

        Raises:
            ValueError: sections is a string, or a selected key is unregistered.
        """
        if isinstance(sections, str):
            raise ValueError("sections must be an iterable of keys, not a string")
        keys = tuple(self.collectors) if sections is None else tuple(dict.fromkeys(sections))
        for key in keys:
            if key not in self.collectors:
                raise ValueError(f"Unknown collector key: {key}")
        return keys

    def validate(self, key: str, data: object) -> None:
        """Check a branch's result_type without copying it or inspecting fields.

        Subclasses of the declared dataclass are accepted. This does not check
        field annotations, nested values or freshness metadata.

        Raises:
            ValueError: The key is unregistered or data has an incompatible type.
        """
        self.select([key])
        model = self.collectors[key].result_type
        if not isinstance(data, model):
            raise ValueError(f"Collector {key} must return {model.__qualname__}")
