"""Built-in collectors sharing the facts package source bundle."""

from .apt import AptFacts
from .cpu import CpuFacts
from .memory import MemoryFacts
from .network import NetworkdFacts, NetworkFacts
from .pacman import PacmanFacts
from .python import PythonFacts
from .storage import StorageFacts
from .system import SystemFacts
from .systemd import SystemdFacts, SystemdResolvedFacts, SystemdTimesyncFacts

__all__ = [
    "AptFacts",
    "CpuFacts",
    "MemoryFacts",
    "PacmanFacts",
    "NetworkFacts",
    "NetworkdFacts",
    "PythonFacts",
    "StorageFacts",
    "SystemFacts",
    "SystemdFacts",
    "SystemdResolvedFacts",
    "SystemdTimesyncFacts",
]
