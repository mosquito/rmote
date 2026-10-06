"""Processor facts: model, architecture, counts, topology, caches and mitigations.

Every source is a kernel file or a standard library call. On a target without
the processor table of Linux the branch reports the counts that ``sysconf``
gives. No command runs and no privileges are needed.
"""

import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rmote.protocol import Tool

from . import cgroup, posix

CPU_ROOT = Path("/sys/devices/system/cpu")
NODE_ROOT = Path("/sys/devices/system/node")
SIZE_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}


@dataclass
class CpuCache:
    """One cache of the first processor, with the processors that share it."""

    level: int
    kind: str
    size_bytes: int | None
    shared_with: str | None


@dataclass
class CpuTopology:
    """How the online processors are arranged.

    ``logical`` counts the online processors. The range strings keep the
    kernel's own notation, so a sparse set stays readable.
    """

    logical: int | None
    sockets: int | None
    cores_per_socket: int | None
    threads_per_core: int | None
    numa_nodes: int | None
    smt: bool | None
    online: str | None
    present: str | None
    offline: str | None
    possible: str | None


@dataclass
class CpuFrequency:
    """Frequency policy of the first processor, in kilohertz."""

    governor: str | None
    driver: str | None
    minimum_khz: int | None
    maximum_khz: int | None
    current_mhz: float | None


@dataclass
class CpuInfo:
    """Processor facts of the target.

    ``available`` reports whether the kernel exposes its processor table. A
    target without it still reports the architecture, the usable count and the
    online count in ``topology``, so False means "no Linux processor table"
    and not "no data at all".
    ``usable`` is how many processors this process may run on, which a CPU
    affinity or a control group can reduce below the installed count.
    ``quota`` is the control group limit expressed as a number of processors.
    It is the strictest ``cpu.max`` of the groups the target shows, and None
    means that none of them limits the process.

    ``model`` is absent on an architecture whose ``/proc/cpuinfo`` gives no
    model name, for example arm64. ``vendor`` then carries the
    ``CPU implementer`` identifier.

    Each section is None when its source is absent, which separates an
    unsupported target from an empty but successful read. ``raw`` keeps the
    text of every source that was read.
    """

    available: bool
    architecture: str
    model: str | None = None
    vendor: str | None = None
    family: str | None = None
    stepping: str | None = None
    microcode: str | None = None
    usable: int | None = None
    quota: float | None = None
    topology: CpuTopology | None = None
    frequency: CpuFrequency | None = None
    caches: list[CpuCache] | None = None
    flags: list[str] | None = None
    bugs: list[str] | None = None
    vulnerabilities: dict[str, str] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class CpuFacts(Tool):
    """Collect the processor model, architecture, counts and mitigations.

    Owns the ``cpu`` key. Model, vendor and flags come from ``/proc/cpuinfo``,
    counts and topology from ``/sys/devices/system/cpu``, and the state of each
    hardware mitigation from its ``vulnerabilities`` directory.

    The installed count and the usable count are reported separately. A
    container or a CPU affinity reduces what the process may use without
    changing what the machine has.

    ``/proc/cpuinfo`` repeats almost every field for each processor, so only
    the first block is kept; the per-processor values that do differ are the
    current frequency and the identifiers that topology already reports.
    """

    key = "cpu"
    version = 1
    result_type = CpuInfo

    @staticmethod
    def read_text(path: Path) -> str | None:
        """Read one kernel file, treating an absent or refused source as None."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @staticmethod
    def attribute(path: Path) -> str | None:
        """Read one sysfs attribute without its trailing newline."""
        text = CpuFacts.read_text(path)
        return text.strip() if text is not None else None

    @staticmethod
    def number(path: Path) -> int | None:
        """Read one numeric sysfs attribute."""
        value = CpuFacts.attribute(path)
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def count_range(value: str | None) -> int | None:
        """Count the processors of a kernel range such as ``0-3,8``."""
        if not value:
            return None
        total = 0
        for part in value.split(","):
            start, separator, end = part.partition("-")
            try:
                first = int(start)
                total += int(end) - first + 1 if separator else 1
            except ValueError:
                return None
        return total

    @staticmethod
    def parse_size(value: str | None) -> int | None:
        """Convert a sysfs size such as ``16384K`` into bytes."""
        if not value:
            return None
        match = re.fullmatch(r"(\d+)\s*([KMG])?B?", value.strip())
        if match is None:
            return None
        return int(match.group(1)) * SIZE_UNITS.get(match.group(2) or "", 1)

    @staticmethod
    def parse_cpuinfo(text: str) -> list[dict[str, str]]:
        """Split /proc/cpuinfo into one record for each processor."""
        records = []
        current: dict[str, str] = {}
        for line in text.splitlines():
            if not line.strip():
                if current:
                    records.append(current)
                    current = {}
                continue
            name, separator, value = line.partition(":")
            if separator:
                current[name.strip()] = value.strip()
        if current:
            records.append(current)
        return records

    @staticmethod
    def read_topology(records: list[dict[str, str]], root: Path = CPU_ROOT) -> CpuTopology:
        """Describe the arrangement of the online processors.

        Sockets and cores come from the identifiers of ``/proc/cpuinfo`` when
        it reports them. An architecture that omits them leaves those counts
        None instead of a guess.
        """
        online = CpuFacts.attribute(root / "online")
        sockets = {record["physical id"] for record in records if "physical id" in record}
        cores = {record.get("cpu cores") for record in records if "cpu cores" in record}
        logical = CpuFacts.count_range(online) or (len(records) or None)
        cores_per_socket = None
        if len(cores) == 1:
            cores_per_socket = CpuFacts.number_of(next(iter(cores)))
        threads = None
        if cores_per_socket and sockets and logical:
            total_cores = cores_per_socket * len(sockets)
            threads = logical // total_cores if total_cores and logical % total_cores == 0 else None
        nodes = sorted(item.name for item in NODE_ROOT.glob("node*")) if NODE_ROOT.exists() else []
        return CpuTopology(
            logical=logical,
            sockets=len(sockets) or None,
            cores_per_socket=cores_per_socket,
            threads_per_core=threads,
            numa_nodes=len(nodes) or None,
            smt=CpuFacts.smt(root),
            online=online,
            present=CpuFacts.attribute(root / "present"),
            offline=CpuFacts.attribute(root / "offline") or None,
            possible=CpuFacts.attribute(root / "possible"),
        )

    @staticmethod
    def number_of(value: str | None) -> int | None:
        """Convert a /proc/cpuinfo value to an integer, or None."""
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def smt(root: Path = CPU_ROOT) -> bool | None:
        """Report whether simultaneous multithreading is active."""
        value = CpuFacts.attribute(root / "smt" / "active")
        return None if value is None else value == "1"

    @staticmethod
    def read_frequency(record: dict[str, str], root: Path = CPU_ROOT) -> CpuFrequency:
        """Read the frequency policy of the first processor.

        A target without the cpufreq driver, such as most virtual machines,
        reports only the current frequency of ``/proc/cpuinfo``.
        """
        policy = root / "cpu0" / "cpufreq"
        current = record.get("cpu MHz")
        try:
            megahertz = float(current) if current else None
        except ValueError:
            megahertz = None
        return CpuFrequency(
            governor=CpuFacts.attribute(policy / "scaling_governor"),
            driver=CpuFacts.attribute(policy / "scaling_driver"),
            minimum_khz=CpuFacts.number(policy / "cpuinfo_min_freq"),
            maximum_khz=CpuFacts.number(policy / "cpuinfo_max_freq"),
            current_mhz=megahertz,
        )

    @staticmethod
    def read_caches(root: Path = CPU_ROOT) -> list[CpuCache] | None:
        """List the caches of the first processor, ordered by level."""
        base = root / "cpu0" / "cache"
        try:
            indexes = sorted(base.glob("index*"), key=lambda item: item.name)
        except OSError:
            return None
        if not indexes:
            return None
        caches = []
        for index in indexes:
            level = CpuFacts.number(index / "level")
            kind = CpuFacts.attribute(index / "type")
            if level is None or kind is None:
                continue
            caches.append(
                CpuCache(
                    level=level,
                    kind=kind,
                    size_bytes=CpuFacts.parse_size(CpuFacts.attribute(index / "size")),
                    shared_with=CpuFacts.attribute(index / "shared_cpu_list"),
                )
            )
        return caches

    @staticmethod
    def read_vulnerabilities(root: Path = CPU_ROOT) -> dict[str, str] | None:
        """Report the state of every hardware mitigation the kernel knows."""
        base = root / "vulnerabilities"
        try:
            items = sorted(base.iterdir())
        except OSError:
            return None
        result = {}
        for item in items:
            value = CpuFacts.attribute(item)
            if value is not None:
                result[item.name] = value
        return result

    @staticmethod
    def read_quota() -> float | None:
        """Express the strictest control group CPU limit as processors.

        Every group of the chain enforces its own ``cpu.max``, and the periods
        can differ, so each pair becomes a number of processors before the
        comparison. ``max``, absent and malformed files declare no limit and
        are skipped. None means that no visible group limits the process.
        """
        found = []
        for value in cgroup.values("cpu.max"):
            parts = value.split()
            if len(parts) != 2 or parts[0] == "max":
                continue
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if period > 0:
                found.append(quota / period)
        return min(found, default=None)

    @staticmethod
    def usable_count() -> int | None:
        """Count the processors this process may run on.

        CPU affinity exists on Linux only, so the installed count is the
        answer elsewhere.
        """
        affinity = getattr(os, "sched_getaffinity", None)
        if affinity is None:
            return os.cpu_count()
        try:
            return len(affinity(0))
        except OSError:
            return os.cpu_count()

    @staticmethod
    def portable_topology() -> CpuTopology | None:
        """Report the online processors of a target without the kernel table.

        ``SC_NPROCESSORS_ONLN`` counts them on every POSIX target. The fields
        that only the kernel table gives stay None: no portable call reports
        sockets, cores or the ranges.
        """
        logical = posix.sysconf("SC_NPROCESSORS_ONLN")
        if logical is None:
            return None
        return CpuTopology(
            logical=logical,
            sockets=None,
            cores_per_socket=None,
            threads_per_core=None,
            numa_nodes=None,
            smt=None,
            online=None,
            present=None,
            offline=None,
            possible=None,
        )

    @staticmethod
    def collect(*, raw: bool = True) -> CpuInfo:
        """Return the processor facts of the machine executing this method.

        Without *raw* the branch keeps every parsed field and drops the text of
        ``/proc/cpuinfo``, which is about nine tenths of its size.
        """
        architecture = platform.machine()
        if platform.system() != "Linux" or (text := CpuFacts.read_text(Path("/proc/cpuinfo"))) is None:
            return CpuInfo(
                available=False,
                architecture=architecture,
                usable=CpuFacts.usable_count(),
                topology=CpuFacts.portable_topology(),
            )
        records = CpuFacts.parse_cpuinfo(text)
        first = records[0] if records else {}
        flags = first.get("flags") or first.get("Features")
        bugs = first.get("bugs")
        return CpuInfo(
            available=True,
            architecture=architecture,
            model=first.get("model name") or first.get("Model") or None,
            vendor=first.get("vendor_id") or first.get("CPU implementer") or None,
            family=first.get("cpu family") or None,
            stepping=first.get("stepping") or None,
            microcode=first.get("microcode") or None,
            usable=CpuFacts.usable_count(),
            quota=CpuFacts.read_quota(),
            topology=CpuFacts.read_topology(records),
            frequency=CpuFacts.read_frequency(first),
            caches=CpuFacts.read_caches(),
            flags=sorted(flags.split()) if flags else None,
            bugs=sorted(bugs.split()) if bugs else None,
            vulnerabilities=CpuFacts.read_vulnerabilities(),
            raw={"cpuinfo": text} if raw else {},
        )


__tool_package__ = "rmote.tools.facts"
