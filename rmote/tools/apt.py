import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from rmote.process import async_process
from rmote.protocol import Tool
from rmote.requires import needs_program


class State(IntEnum):
    PRESENT = 0
    ABSENT = 1
    LATEST = 2


@dataclass
class Package:
    name: str
    state: int = State.PRESENT
    version: str = ""

    @classmethod
    def parse(cls, s: str | object, state: State | int | None = None) -> "Package":
        """Use the explicit state, then Package.state, then PRESENT."""
        if isinstance(s, cls):
            return cls(name=s.name, version=s.version, state=State(s.state if state is None else state))
        if not isinstance(s, str):
            raise TypeError(f"Expected str or Package, got {type(s).__name__!r}")
        return cls.from_string(s, state=State.PRESENT if state is None else State(state))

    @classmethod
    def from_string(cls, s: str, state: State = State.PRESENT) -> "Package":
        if "=" in s:
            name, version = s.split("=", 1)
            return cls(name=name, version=version, state=state)
        return cls(name=s, version="", state=state)

    def __str__(self) -> str:
        return f"{self.name}={self.version}" if self.version else self.name


@dataclass
class Result:
    name: str
    changed: bool
    version: str = ""

    @classmethod
    def from_package(cls, package: "Package", changed: bool) -> "Result":
        return cls(name=package.name, version=package.version, changed=changed)


class Backend:
    DPKG_STATUS: ClassVar[Path] = Path("/var/lib/dpkg/status")

    @staticmethod
    def parse_deb822(text: str) -> "tuple[Mapping[str, str], ...]":
        """Parse a deb822-formatted string into a list of field dicts."""
        logging.debug("Parsing deb822-formatted string")
        stanzas = []
        for stanza in text.split("\n\n"):
            fields: dict[str, str] = {}
            for line in stanza.splitlines():
                key, sep, value = line.partition(": ")
                if sep:
                    fields[key] = value
            if fields:
                stanzas.append(MappingProxyType(fields))
        return tuple(stanzas)

    @classmethod
    def read_status(cls) -> Mapping[str, Mapping[str, str]]:
        try:
            logging.debug("Reading state from %s", cls.DPKG_STATUS)
            return MappingProxyType(
                {s["Package"]: s for s in cls.parse_deb822(cls.DPKG_STATUS.read_text()) if "Package" in s}
            )
        except FileNotFoundError:
            return MappingProxyType({})

    @staticmethod
    async def apt_get(*args: str) -> tuple[int, str, str]:
        needs_program("Apt", "apt-get", "Debian and Ubuntu")
        logging.debug("calling apt-get with args: %s", args)
        result = await async_process(
            "apt-get",
            "-y",
            *args,
            capture_output=True,
            text=True,
            env={"DEBIAN_FRONTEND": "noninteractive", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def parse_upgradable(stdout: str) -> list[str]:
        packages = []
        for line in stdout.splitlines():
            if line.startswith("Inst "):
                parts = line.split()
                if len(parts) >= 2:
                    packages.append(parts[1])
        return packages


class Apt(Tool):
    """Manage Debian/Ubuntu packages via apt-get. Requires root on the remote host.

    Every operation that changes packages runs ``apt-get``. A target without
    it refuses with NotImplementedError. The installed set is read from
    ``/var/lib/dpkg/status``, and an absent file reads as no packages.

    Install and remove a package in a disposable Debian container. ``remote``
    is the fixture's synchronous RPC callable (like a ``Connection``)::

        >>> remote = getfixture("debian_tool")
        >>> remote(Apt.update)
        True
        >>> remote(Apt.package, "nano").changed
        True
        >>> remote(Apt.package, "nano").changed
        False
        >>> remote(Apt.package, "nano", state=State.ABSENT).changed
        True
        >>> remote(Apt.package, "nano", state=State.ABSENT).changed
        False
    """

    @staticmethod
    async def update(ttl: int | float = -1) -> bool:
        """Run ``apt-get update``, with optional TTL-based skipping.

        Args:
            ttl: Minimum age in seconds of the last successful update before
                re-running. ``-1`` (default) always runs the update.

        Returns:
            ``True`` if ``apt-get update`` was executed, ``False`` if the
            cache is fresher than *ttl* seconds.

        Raises:
            RuntimeError: If ``apt-get update`` exits with a non-zero status.
        """
        stamp = Path("/var/lib/apt/lists/.rmote-update")
        if ttl >= 0:
            try:
                age = time.time() - stamp.stat().st_mtime
            except FileNotFoundError:
                age = float("inf")
            if age < ttl:
                return False
        rc, _, err = await Backend.apt_get("update")
        if rc != 0:
            raise RuntimeError(f"apt-get update failed:\n{err}")
        stamp.touch()
        return True

    @classmethod
    async def package(cls, package: str | Package, state: State | int | None = None) -> Result:
        """Install, remove, or upgrade a single package.

        Args:
            package: Package name, ``name=version`` string, or a
                :class:`Package` instance.
            state: Desired state - :attr:`State.PRESENT` (install if absent),
                :attr:`State.ABSENT` (purge if installed), or
                :attr:`State.LATEST` (install or upgrade to candidate version).
                An explicit state overrides Package.state. If omitted, use
                Package.state for objects or PRESENT for strings.

        Returns:
            :class:`Result` with the package name, installed version, and
            whether the system was changed.

        Raises:
            RuntimeError: If the underlying ``apt-get`` invocation fails.
        """
        package = Package.parse(package, state=state)
        state = State(package.state)

        status = Backend.read_status()
        info = status.get(package.name, {})
        installed = info.get("Status", "") == "install ok installed"
        version = info.get("Version", "")

        if state == State.PRESENT:
            if installed:
                return Result(name=package.name, version=version, changed=False)
            rc, _, err = await Backend.apt_get("install", str(package))
            if rc != 0:
                raise RuntimeError(f"apt-get install {package!r} failed:\n{err}")
            version = Backend.read_status().get(package.name, {}).get("Version", "")
            return Result(name=package.name, version=version, changed=True)

        if state == State.ABSENT:
            if not installed:
                return Result(name=package.name, version="", changed=False)
            rc, _, err = await Backend.apt_get("remove", "--purge", str(package))
            if rc != 0:
                raise RuntimeError(f"apt-get remove {package!r} failed:\n{err}")
            return Result(name=package.name, version="", changed=True)

        if state == State.LATEST:
            if not installed:
                rc, _, err = await Backend.apt_get("install", str(package))
                if rc != 0:
                    raise RuntimeError(f"apt-get install {package!r} failed:\n{err}")
                version = Backend.read_status().get(package.name, {}).get("Version", "")
                return Result(name=package.name, version=version, changed=True)
            _, sim_out, _ = await Backend.apt_get("install", "--simulate", str(package))
            if package.name not in Backend.parse_upgradable(sim_out):
                return Result(name=package.name, version=version, changed=False)
            rc, _, err = await Backend.apt_get("install", str(package))
            if rc != 0:
                raise RuntimeError(f"apt-get upgrade {package!r} failed:\n{err}")
            version = Backend.read_status().get(package.name, {}).get("Version", "")
            return Result(name=package.name, version=version, changed=True)

        raise ValueError(f"Unknown state: {state!r}")

    @classmethod
    async def converge(cls, *packages: str | Package) -> list[Result]:
        """Apply each Package.state; string arguments default to PRESENT.

        Args:
            *packages: Package names, ``name=version`` strings, or
                :class:`Package` instances.

        Returns:
            List of :class:`Result` objects, one per package, in the same order
            as the input.
        """
        return [await cls.package(pkg) for pkg in packages]
