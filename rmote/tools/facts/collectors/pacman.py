"""Read-only Arch package inventory."""

import os
import shutil
from dataclasses import dataclass

from rmote.process import async_process
from rmote.protocol import Tool


@dataclass
class PacmanPackage:
    version: str


@dataclass
class PacmanInfo:
    available: bool
    version: str | None = None
    packages: dict[str, PacmanPackage] | None = None


class PacmanFacts(Tool):
    """Collect pacman version and installed packages under the ``pacman`` key.

    Missing pacman produces ``available=False`` and None for version/packages.
    ``packages`` maps each installed name to its version, including locally
    installed/foreign packages. ``version`` is the full pacman --version output.
    Reads only the local database using -Q: no refresh, upgrades or network.
    Each command has a 30-second timeout; command errors propagate.
    """

    key = "pacman"
    version = 1
    result_type = PacmanInfo

    @staticmethod
    def packages(output: str) -> dict[str, PacmanPackage]:
        """Parse pacman's name/version inventory without altering versions."""
        result = {}
        for line in output.splitlines():
            name, version = line.split(maxsplit=1)
            result[name] = PacmanPackage(version)
        return result

    @staticmethod
    async def collect() -> PacmanInfo:
        """Return the installed inventory without modifying pacman's database."""
        executable = shutil.which("pacman")
        result: PacmanInfo = PacmanInfo(available=executable is not None, version=None, packages=None)
        if executable is None:
            return result
        options = {**os.environ, "LC_ALL": "C"}
        version = await async_process(
            executable, "--version", capture_output=True, text=True, check=True, timeout=30, env=options
        )
        inventory = await async_process(
            executable, "-Q", capture_output=True, text=True, check=True, timeout=30, env=options
        )
        result.version = version.stdout.strip()
        result.packages = PacmanFacts.packages(inventory.stdout)
        return result


__tool_package__ = "rmote.tools.facts"
