"""Read-only Debian package inventory."""

import os
import shutil
from dataclasses import dataclass

from rmote.process import async_process
from rmote.protocol import Tool


@dataclass
class AptPackage:
    version: str
    architecture: str
    status: str


@dataclass
class AptInfo:
    available: bool
    version: str | None = None
    packages: dict[str, AptPackage] | None = None


class AptFacts(Tool):
    """Collect APT version and installed packages under the ``apt`` key.

    Requires both apt-get and dpkg-query. Missing binaries produce
    ``available=False`` and None for version/packages. Each package is indexed
    by dpkg's binary package name (including the architecture qualifier when
    applicable), retaining version, architecture and the full dpkg status.
    Removed/config-files-only and incompletely installed packages are excluded.
    Installed packages on hold are included.

    Queries do not refresh indexes, contact repositories or acquire a write
    lock. Each command has a 30-second timeout; errors from an installed utility
    propagate. ``version`` is the complete apt-get --version output.
    """

    key = "apt"
    version = 1
    result_type = AptInfo

    @staticmethod
    def packages(output: str) -> dict[str, AptPackage]:
        """Parse tab-separated dpkg records, preserving multiarch identities."""
        result = {}
        for line in output.splitlines():
            name, version, architecture, status = line.split("\t", 3)
            if status.split()[1:] != ["ok", "installed"]:
                continue
            result[name] = AptPackage(version, architecture, status)
        return result

    @staticmethod
    async def collect() -> AptInfo:
        """Return local package state without changing the package database."""
        executable = shutil.which("apt-get")
        query = shutil.which("dpkg-query")
        result: AptInfo = AptInfo(available=executable is not None and query is not None, version=None, packages=None)
        if executable is None or query is None:
            return result
        options = {**os.environ, "LC_ALL": "C"}
        version = await async_process(
            executable, "--version", capture_output=True, text=True, check=True, timeout=30, env=options
        )
        inventory = await async_process(
            query,
            "--show",
            "--showformat=${binary:Package}\t${Version}\t${Architecture}\t${Status}\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=options,
        )
        result.version = version.stdout.strip()
        result.packages = AptFacts.packages(inventory.stdout)
        return result


__tool_package__ = "rmote.tools.facts"
