"""Read cgroup v2 files for the running process's CPU and memory collectors.

A limit lookup reads every group from the process's own group to the mount
root and reports the strictest value, because the kernel enforces the limit of
each ancestor. It reads only the groups the target shows, so None means "no
visible limit" and not "unrestricted". A usage lookup reads the process's own
group alone, because a parent file counts the processes of other groups.
Version 1 controllers are not read.
"""

from collections.abc import Iterator
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROCESS = Path("/proc/self/cgroup")


def read_text(path: Path) -> str | None:
    """Read one kernel file, treating an absent or refused source as None."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def own_path(root: Path = CGROUP_ROOT, process: Path = PROCESS) -> Path | None:
    """Return the control group directory of this process, version 2 only.

    The file holds ``0::<path>`` for version 2. Version 1 splits the limits
    across controllers and is not read here.

    Inside a cgroup namespace the path starts at the namespace root, so it
    names the deepest group the target shows and not the group of the host.
    """
    text = read_text(process)
    if text is None:
        return None
    for line in text.splitlines():
        identifier, _, rest = line.partition(":")
        controller, _, path = rest.partition(":")
        if identifier == "0" and not controller:
            return root / path.strip("/")
    return None


def values(name: str, root: Path = CGROUP_ROOT, process: Path = PROCESS) -> Iterator[str]:
    """Yield each readable *name* file from the process's group up to *root*.

    Values arrive stripped, nearest group first. A missing or unreadable file
    is skipped and the walk continues, so a group that declares nothing hides
    no ancestor. The walk ends at *root* and never reads a group above it.
    """
    directory = own_path(root, process)
    if directory is None:
        return
    while True:
        value = read_text(directory / name)
        if value is not None:
            yield value.strip()
        if directory == root or root not in directory.parents:
            return
        directory = directory.parent


def own_value(name: str, root: Path = CGROUP_ROOT, process: Path = PROCESS) -> str | None:
    """Read *name* in the process's own group, without reading an ancestor.

    A usage file counts every process of the group that holds it, so a parent
    value describes other workloads too. Use this for a usage, and
    :func:`values` for a limit.
    """
    directory = own_path(root, process)
    if directory is None:
        return None
    value = read_text(directory / name)
    return None if value is None else value.strip()


def as_bytes(value: str | None) -> int | None:
    """Convert a byte count. ``max``, absent and malformed text give None."""
    if value is None or value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def bytes_limit(name: str, root: Path = CGROUP_ROOT, process: Path = PROCESS) -> int | None:
    """Report the strictest byte limit of the groups the target shows.

    ``max``, absent and malformed files declare no limit and are skipped, so
    a child that declares ``max`` still reports the finite limit of a parent.
    None means that no visible group limits the process, which an ancestor
    outside a cgroup namespace can still do.
    """
    found = [number for number in (as_bytes(value) for value in values(name, root, process)) if number is not None]
    return min(found, default=None)


def own_bytes(name: str, root: Path = CGROUP_ROOT, process: Path = PROCESS) -> int | None:
    """Read a byte count of the process's own group, such as a usage."""
    return as_bytes(own_value(name, root, process))
