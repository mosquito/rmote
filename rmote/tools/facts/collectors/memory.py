"""Memory facts: what the kernel manages, what is free and what swap exists.

The source is ``/proc/meminfo`` and the control group limits of the running
process. On another POSIX target the branch reports the memory of the machine
through ``sysconf``, which every such target answers. No command runs and no
privileges are needed.
"""

import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rmote.protocol import Tool

from . import cgroup, posix

MEMINFO = Path("/proc/meminfo")


@dataclass
class SwapMemory:
    """Swap totals, in bytes."""

    total_bytes: int
    free_bytes: int
    used_bytes: int
    cached_bytes: int | None


@dataclass
class HugePages:
    """Huge page pool of the default size."""

    size_bytes: int | None
    total: int | None
    free: int | None
    reserved: int | None


@dataclass
class MemoryInfo:
    """Memory facts of the target, in bytes.

    ``total_bytes`` is what the kernel manages, which is slightly below the
    installed hardware because firmware and the kernel reserve some of it.
    ``available_bytes`` is the kernel's own estimate of what a new workload
    can take without swapping, and it is larger than ``free_bytes`` because
    reclaimable caches count towards it.

    ``limit_bytes`` and ``usage_bytes`` come from the control group, so a
    container reports what it may use next to what the machine has.
    ``limit_bytes`` is the strictest ``memory.max`` of the groups the target
    shows, and None means that none of them limits the process.
    ``usage_bytes`` is the ``memory.current`` of the process's own group, so
    it never counts the processes of a parent group.

    ``available`` reports whether ``/proc/meminfo`` was read. A target without
    it still reports ``total_bytes``, and ``free_bytes`` when its ``sysconf``
    names the free pages, so False means "no Linux memory table" and not "no
    data at all".
    """

    available: bool
    total_bytes: int | None = None
    free_bytes: int | None = None
    available_bytes: int | None = None
    used_bytes: int | None = None
    buffers_bytes: int | None = None
    cached_bytes: int | None = None
    shared_bytes: int | None = None
    swap: SwapMemory | None = None
    hugepages: HugePages | None = None
    commit_limit_bytes: int | None = None
    committed_bytes: int | None = None
    limit_bytes: int | None = None
    usage_bytes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MemoryFacts(Tool):
    """Collect installed, free and available memory, swap and huge pages.

    Owns the ``memory`` key. ``/proc/meminfo`` reports its values in
    kibibytes, and every field here is bytes. ``used_bytes`` is derived as
    total minus available, which is what a workload actually finds occupied;
    it is not total minus free, because caches are reclaimable.

    The complete ``/proc/meminfo`` is kept in ``raw``, so a field this model
    does not name is still available.

    A target without ``/proc/meminfo`` reports what ``sysconf`` gives: the
    memory of the machine, and the free pages where the name exists. The other
    fields stay None, because no portable call reports them.
    """

    key = "memory"
    version = 1
    result_type = MemoryInfo

    @staticmethod
    def read_text(path: Path = MEMINFO) -> str | None:
        """Read the memory table, treating an absent source as None."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @staticmethod
    def parse(text: str) -> dict[str, int]:
        """Parse /proc/meminfo into bytes, keeping its original field names.

        A field in kibibytes is multiplied; a plain count stays as it is.
        """
        result = {}
        for line in text.splitlines():
            name, separator, rest = line.partition(":")
            if not separator:
                continue
            parts = rest.split()
            if not parts:
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            result[name.strip()] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
        return result

    @staticmethod
    def read_swap(values: dict[str, int]) -> SwapMemory | None:
        """Report swap totals, or None when the target has no swap table."""
        total = values.get("SwapTotal")
        free = values.get("SwapFree")
        if total is None or free is None:
            return None
        return SwapMemory(
            total_bytes=total,
            free_bytes=free,
            used_bytes=total - free,
            cached_bytes=values.get("SwapCached"),
        )

    @staticmethod
    def read_hugepages(values: dict[str, int]) -> HugePages | None:
        """Report the huge page pool, or None when the target has none."""
        if "Hugepagesize" not in values and "HugePages_Total" not in values:
            return None
        return HugePages(
            size_bytes=values.get("Hugepagesize"),
            total=values.get("HugePages_Total"),
            free=values.get("HugePages_Free"),
            reserved=values.get("HugePages_Rsvd"),
        )

    @staticmethod
    def portable() -> MemoryInfo:
        """Report what every POSIX target gives, for a target without meminfo.

        ``SC_PHYS_PAGES`` counts the pages the kernel manages, which is what
        ``MemTotal`` reports on Linux. ``SC_AVPHYS_PAGES`` counts the pages
        that are free now; macOS has no such name and gives None.
        """
        size = posix.sysconf("SC_PAGE_SIZE")
        total = posix.sysconf("SC_PHYS_PAGES")
        free = posix.sysconf("SC_AVPHYS_PAGES")
        return MemoryInfo(
            available=False,
            total_bytes=total * size if total is not None and size is not None else None,
            free_bytes=free * size if free is not None and size is not None else None,
        )

    @staticmethod
    def collect(*, raw: bool = True) -> MemoryInfo:
        """Return the memory facts of the machine executing this method."""
        if platform.system() != "Linux":
            return MemoryFacts.portable()
        text = MemoryFacts.read_text()
        if text is None:
            return MemoryFacts.portable()
        values = MemoryFacts.parse(text)
        total = values.get("MemTotal")
        usable = values.get("MemAvailable")
        return MemoryInfo(
            available=True,
            total_bytes=total,
            free_bytes=values.get("MemFree"),
            available_bytes=usable,
            used_bytes=total - usable if total is not None and usable is not None else None,
            buffers_bytes=values.get("Buffers"),
            cached_bytes=values.get("Cached"),
            shared_bytes=values.get("Shmem"),
            swap=MemoryFacts.read_swap(values),
            hugepages=MemoryFacts.read_hugepages(values),
            commit_limit_bytes=values.get("CommitLimit"),
            committed_bytes=values.get("Committed_AS"),
            limit_bytes=cgroup.bytes_limit("memory.max"),
            usage_bytes=cgroup.own_bytes("memory.current"),
            raw={"meminfo": text} if raw else {},
        )


__tool_package__ = "rmote.tools.facts"
