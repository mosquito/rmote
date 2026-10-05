"""Operating system facts, collected without external commands."""

import platform
import shlex
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rmote.protocol import Tool


@dataclass
class DistributionInfo:
    id: str | None = None
    version_id: str | None = None
    pretty_name: str | None = None


@dataclass
class SystemInfo:
    hostname: str
    os: str
    kernel: str
    architecture: str
    distribution: DistributionInfo | None


class SystemFacts(Tool):
    """Collect hostname, OS, kernel, architecture and Linux distribution.

    Owns the ``system`` key. Distribution is ``None`` outside Linux or when
    os-release is absent. Missing distribution fields are ``None``.
    No privileges or third-party packages are required.
    """

    key = "system"
    version = 1
    result_type = SystemInfo

    @staticmethod
    def read_distribution(
        paths: Iterable[Path] = (Path("/etc/os-release"), Path("/usr/lib/os-release")),
    ) -> DistributionInfo | None:
        """Read current os-release data without platform's process-lifetime cache.

        The first existing file wins. Only the three published distribution fields
        are parsed; shell code and variable expansion are never evaluated.
        """
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            result: dict[str, Any] = {"id": None, "version_id": None, "pretty_name": None}
            for line in lines:
                name, separator, value = line.partition("=")
                key = name.lower()
                if separator and key in result:
                    words = shlex.split(value, comments=True)
                    if len(words) > 1:
                        raise ValueError(f"Invalid os-release value for {name}")
                    result[key] = words[0] if words else ""
            return DistributionInfo(**result)
        return None

    @staticmethod
    def collect() -> SystemInfo:
        """Return facts from the machine executing this method."""
        return SystemInfo(
            hostname=socket.gethostname(),
            os=platform.system(),
            kernel=platform.release(),
            architecture=platform.machine(),
            distribution=SystemFacts.read_distribution() if platform.system() == "Linux" else None,
        )


__tool_package__ = "rmote.tools.facts"
