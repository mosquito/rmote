"""Storage facts: mounted filesystems, their space, block devices and swap.

Every source is a kernel file or a standard library call. The space of the
root filesystem comes from ``os.statvfs``, which every POSIX target answers,
so a target without the mount table of Linux still reports it. No external
command runs, and no privileges are needed.
"""

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rmote.protocol import Tool

# Filesystems that hold no stored data and report no meaningful space. They
# stay in the result with ``pseudo`` set, so a caller keeps the full picture
# and selects what it needs.
PSEUDO_FILESYSTEMS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "efivarfs",
        "fuse.gvfsd-fuse",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "proc",
        "pstore",
        "securityfs",
        "selinuxfs",
        "sysfs",
        "tracefs",
    }
)

# A sector of a block device is always 512 bytes in sysfs, whatever the
# logical block size of the hardware.
SYSFS_SECTOR = 512


@dataclass
class FilesystemUsage:
    """Space and inodes of one mounted filesystem, in bytes and counts.

    ``free_bytes`` is what the filesystem reports as unused, and
    ``available_bytes`` is what an unprivileged process can still use. The two
    differ when the filesystem reserves space for the superuser.
    """

    total_bytes: int
    free_bytes: int
    available_bytes: int
    total_inodes: int | None
    free_inodes: int | None
    available_inodes: int | None


@dataclass
class MountPoint:
    """One mounted filesystem, as the kernel lists it.

    ``usage`` is None for a pseudo filesystem, which is not queried, or when
    ``os.statvfs`` raises OSError, such as a permission error. An unresponsive
    mount can instead block collection: there is no per-mount timeout that
    would turn a pending call into None.
    """

    target: str
    source: str
    fstype: str
    options: list[str]
    super_options: list[str]
    device: str | None
    root: str
    read_only: bool
    pseudo: bool
    usage: FilesystemUsage | None


@dataclass
class BlockDevice:
    """One whole block device from sysfs, with the sizes of its partitions."""

    name: str
    size_bytes: int | None
    logical_block_size: int | None
    rotational: bool | None
    removable: bool | None
    model: str | None
    partitions: dict[str, int | None]


@dataclass
class SwapArea:
    """One active swap area. Sizes come from /proc/swaps, in bytes."""

    path: str
    kind: str
    size_bytes: int
    used_bytes: int
    priority: int


@dataclass
class StorageInfo:
    """Storage of the target: mounts with their space, devices and swap.

    ``available`` reports whether the kernel exposes its mount table. Each
    section is None when its source is absent, which separates an unsupported
    target from an empty but successful read. ``raw`` keeps the text of every
    source that was read, by source name.

    ``root`` is the space of the root filesystem, which every POSIX target
    reports. It is filled whatever ``available`` says, so a target without the
    mount table of Linux still reports its space. On Linux the same numbers
    are the usage of the ``/`` mount.
    """

    available: bool
    root: FilesystemUsage | None = None
    mounts: list[MountPoint] | None = None
    devices: dict[str, BlockDevice] | None = None
    swaps: list[SwapArea] | None = None
    fstab: list[dict[str, str]] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class StorageFacts(Tool):
    """Collect mounted filesystems, their space, block devices and swap.

    Owns the ``storage`` key. ``available`` reports whether the mount table of
    the kernel could be read; the space of the root filesystem is reported on
    any POSIX target, because ``os.statvfs`` is portable and the mount table is
    not. Mounts come from ``/proc/self/mountinfo``, which
    also gives the device number, the mount root and the shadowing order; the
    older ``/proc/mounts`` is the fallback. Space comes from ``os.statvfs`` for
    each mount, devices from ``/sys/block`` and swap from ``/proc/swaps``.

    Every mount stays in the result, including a pseudo filesystem, which
    carries ``pseudo`` so the caller can select. Mounts keep the order of the
    kernel, so a mount that shadows an earlier one at the same target stays
    visible.

    A single unreadable source leaves its own section None and keeps the
    others. A mount whose ``os.statvfs`` raises OSError retains its record with
    ``usage=None``. An unresponsive mount can block indefinitely instead: there
    is no per-mount timeout, and the collector cannot interrupt the call.

    The collection helpers run this method in a worker thread. Other branches
    can progress, but fetch and gather return only after all selected branches
    succeed. This collector belongs to DEFAULT_COLLECTORS; select other keys
    or a collector set without StorageFacts to avoid querying mounts.
    """

    key = "storage"
    version = 1
    result_type = StorageInfo

    @staticmethod
    def unescape(value: str) -> str:
        """Decode the octal escapes that the kernel writes in a path.

        The kernel escapes space, tab, newline and backslash. Any other
        sequence stays as written.
        """
        if "\\" not in value:
            return value
        result: list[str] = []
        index = 0
        while index < len(value):
            character = value[index]
            if character == "\\" and value[index + 1 : index + 4].isdigit() and len(value) - index >= 4:
                result.append(chr(int(value[index + 1 : index + 4], 8)))
                index += 4
                continue
            result.append(character)
            index += 1
        return "".join(result)

    @staticmethod
    def read_text(path: Path) -> str | None:
        """Read one kernel file, treating an absent or closed source as None."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            return None

    @staticmethod
    def parse_mountinfo(text: str) -> list[dict[str, Any]]:
        """Parse /proc/self/mountinfo, keeping the order of the kernel.

        Fields before the ``-`` separator describe the mount, and the fields
        after it describe the filesystem. The optional fields between them vary
        by kernel, so the separator is found instead of counted.
        """
        records = []
        for line in text.splitlines():
            parts = line.split(" ")
            if "-" not in parts or len(parts) < 10:
                continue
            separator = parts.index("-")
            if separator < 6 or len(parts) < separator + 3:
                continue
            records.append(
                {
                    "device": parts[2],
                    "root": StorageFacts.unescape(parts[3]),
                    "target": StorageFacts.unescape(parts[4]),
                    "options": parts[5].split(","),
                    "fstype": parts[separator + 1],
                    "source": StorageFacts.unescape(parts[separator + 2]),
                    "super_options": parts[separator + 3].split(",") if len(parts) > separator + 3 else [],
                }
            )
        return records

    @staticmethod
    def parse_mounts(text: str) -> list[dict[str, Any]]:
        """Parse the older /proc/mounts, which has no device number or root."""
        records = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            records.append(
                {
                    "device": None,
                    "root": "/",
                    "target": StorageFacts.unescape(parts[1]),
                    "options": parts[3].split(","),
                    "fstype": parts[2],
                    "source": StorageFacts.unescape(parts[0]),
                    "super_options": [],
                }
            )
        return records

    @staticmethod
    def usage(target: str) -> FilesystemUsage | None:
        """Report the space of one mount in bytes, or None on OSError.

        The kernel reports space in fragments, so every number is multiplied by
        the fragment size. A filesystem without inodes reports None for them.
        This synchronous call has no timeout and may block on an unresponsive
        mount; a pending call does not return None.
        """
        try:
            status = os.statvfs(target)
        except OSError:
            return None
        unit = status.f_frsize or status.f_bsize
        return FilesystemUsage(
            total_bytes=status.f_blocks * unit,
            free_bytes=status.f_bfree * unit,
            available_bytes=status.f_bavail * unit,
            total_inodes=status.f_files or None,
            free_inodes=status.f_ffree if status.f_files else None,
            available_inodes=status.f_favail if status.f_files else None,
        )

    @staticmethod
    def read_mounts(
        mountinfo: Path = Path("/proc/self/mountinfo"),
        mounts: Path = Path("/proc/mounts"),
    ) -> tuple[list[MountPoint] | None, dict[str, Any]]:
        """Build the mount list with its space, and return the source text too."""
        raw: dict[str, Any] = {}
        text = StorageFacts.read_text(mountinfo)
        if text is not None:
            raw["mountinfo"] = text
            records = StorageFacts.parse_mountinfo(text)
        else:
            text = StorageFacts.read_text(mounts)
            if text is None:
                return None, raw
            raw["mounts"] = text
            records = StorageFacts.parse_mounts(text)
        result = []
        for record in records:
            pseudo = record["fstype"] in PSEUDO_FILESYSTEMS
            result.append(
                MountPoint(
                    target=record["target"],
                    source=record["source"],
                    fstype=record["fstype"],
                    options=record["options"],
                    super_options=record["super_options"],
                    device=record["device"],
                    root=record["root"],
                    read_only="ro" in record["options"],
                    pseudo=pseudo,
                    usage=None if pseudo else StorageFacts.usage(record["target"]),
                )
            )
        return result, raw

    @staticmethod
    def read_attribute(path: Path) -> str | None:
        """Read one sysfs attribute, or None when it is absent or refused."""
        text = StorageFacts.read_text(path)
        return text.strip() if text is not None else None

    @staticmethod
    def read_number(path: Path, factor: int = 1) -> int | None:
        """Read one numeric sysfs attribute and scale it."""
        value = StorageFacts.read_attribute(path)
        try:
            return int(value) * factor if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def read_flag(path: Path) -> bool | None:
        """Read one boolean sysfs attribute written as 0 or 1."""
        value = StorageFacts.read_number(path)
        return None if value is None else bool(value)

    @staticmethod
    def read_devices(root: Path = Path("/sys/block")) -> dict[str, BlockDevice] | None:
        """Describe every whole block device and the sizes of its partitions."""
        try:
            names = sorted(item.name for item in root.iterdir())
        except OSError:
            return None
        devices = {}
        for name in names:
            base = root / name
            partitions = {}
            try:
                children = sorted(item.name for item in base.iterdir() if (item / "partition").exists())
            except OSError:
                children = []
            for child in children:
                partitions[child] = StorageFacts.read_number(base / child / "size", SYSFS_SECTOR)
            devices[name] = BlockDevice(
                name=name,
                size_bytes=StorageFacts.read_number(base / "size", SYSFS_SECTOR),
                logical_block_size=StorageFacts.read_number(base / "queue" / "logical_block_size"),
                rotational=StorageFacts.read_flag(base / "queue" / "rotational"),
                removable=StorageFacts.read_flag(base / "removable"),
                model=StorageFacts.read_attribute(base / "device" / "model"),
                partitions=partitions,
            )
        return devices

    @staticmethod
    def read_swaps(path: Path = Path("/proc/swaps")) -> tuple[list[SwapArea] | None, str | None]:
        """List active swap areas. The kernel reports their sizes in kibibytes."""
        text = StorageFacts.read_text(path)
        if text is None:
            return None, None
        areas = []
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                size, used, priority = int(parts[2]), int(parts[3]), int(parts[4])
            except ValueError:
                continue
            areas.append(
                SwapArea(
                    path=StorageFacts.unescape(parts[0]),
                    kind=parts[1],
                    size_bytes=size * 1024,
                    used_bytes=used * 1024,
                    priority=priority,
                )
            )
        return areas, text

    @staticmethod
    def read_fstab(path: Path = Path("/etc/fstab")) -> tuple[list[dict[str, str]] | None, str | None]:
        """Read the configured filesystem table, without interpreting it."""
        text = StorageFacts.read_text(path)
        if text is None:
            return None, None
        entries = []
        for line in text.splitlines():
            record = line.split("#", 1)[0].split()
            if len(record) < 3:
                continue
            entries.append(
                {
                    "source": StorageFacts.unescape(record[0]),
                    "target": StorageFacts.unescape(record[1]),
                    "fstype": record[2],
                    "options": record[3] if len(record) > 3 else "defaults",
                    "dump": record[4] if len(record) > 4 else "0",
                    "pass": record[5] if len(record) > 5 else "0",
                }
            )
        return entries, text

    @staticmethod
    def collect(*, raw: bool = True) -> StorageInfo:
        """Return the storage facts of the machine executing this method.

        Without *raw* the branch keeps every parsed field and drops the texts
        of the sources it read.
        """
        root = StorageFacts.usage("/")
        if platform.system() != "Linux":
            return StorageInfo(available=False, root=root)
        mounts, texts = StorageFacts.read_mounts()
        swaps, swaps_text = StorageFacts.read_swaps()
        fstab, fstab_text = StorageFacts.read_fstab()
        if swaps_text is not None:
            texts["swaps"] = swaps_text
        if fstab_text is not None:
            texts["fstab"] = fstab_text
        return StorageInfo(
            available=mounts is not None,
            root=root,
            mounts=mounts,
            devices=StorageFacts.read_devices(),
            swaps=swaps,
            fstab=fstab,
            raw=texts if raw else {},
        )


__tool_package__ = "rmote.tools.facts"
