"""Refusals that name the interface a tool needs and the target it found.

A tool that reads a kernel interface of Linux, or runs the program of one
distribution, cannot work on another target. The error of the interface itself
names a missing file or a missing program, which reads like a damaged host
instead of the wrong one. These checks name the reason, because the failure
travels through RPC where the caller has no other context.

Every check raises NotImplementedError, so a caller that asks several hosts can
tell "this host cannot do it" from "this host tried and failed".
"""

import platform
import shutil
from pathlib import Path

__tool_package__ = "rmote.requires"


def needs_linux(operation: str, source: str) -> None:
    """Refuse *operation* unless this target is Linux.

    Args:
        operation: What the caller asked for, such as ``Sysctl.get``.
        source: The kernel interface that the operation reads or writes.

    Raises:
        NotImplementedError: The target runs another system.
    """
    system = platform.system()
    if system != "Linux":
        raise NotImplementedError(f"{operation} needs the {source} interface of Linux, and this host runs {system}")


def needs_program(operation: str, name: str, provider: str) -> None:
    """Refuse *operation* unless the program *name* is on the PATH.

    Args:
        operation: What the caller asked for, such as ``Service.start``.
        name: The program that the operation runs.
        provider: What installs that program, such as ``systemd``.

    Raises:
        NotImplementedError: The program is absent, so the operation has no way
            to run on this host.
    """
    if shutil.which(name) is None:
        raise NotImplementedError(f"{operation} needs the {name} program of {provider}, and this host has none")


def needs_file(operation: str, path: Path, provider: str) -> None:
    """Refuse *operation* unless the configuration of *provider* is present.

    Args:
        operation: What the caller asked for, such as ``AptRepository.present``.
        path: The file or directory that the configuration of *provider* owns.
        provider: Whose configuration it is, such as ``Debian and Ubuntu``.

    Raises:
        NotImplementedError: The configuration is absent, so this host does not
            have the system the operation configures.
    """
    if not path.exists():
        raise NotImplementedError(f"{operation} needs {path} of {provider}, and this host has none")
