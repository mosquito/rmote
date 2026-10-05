"""Counters that every POSIX target reports through the standard library.

A collector built on a Linux kernel interface reads these values on another
POSIX target, so it fills the fields it can instead of reporting nothing. No
command runs and no privileges are needed.

Every name is asked for on its own, because the set of names differs between
systems: macOS has no ``SC_AVPHYS_PAGES`` and raises for it, while Linux and
FreeBSD answer.
"""

import os


def sysconf(name: str) -> int | None:
    """Read one sysconf value, or None when the target has no such name.

    A negative result means the system leaves the limit unspecified, which is
    not a value, so it becomes None as well.
    """
    try:
        value = os.sysconf(name)
    except (AttributeError, OSError, ValueError):
        return None
    return value if value >= 0 else None
