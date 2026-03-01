import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from rmote.protocol import Tool, process


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
    def parse(cls, s: str | object, state: State | int = State.PRESENT) -> "Package":
        coerced = State(state)
        if isinstance(s, cls):
            return cls(name=s.name, version=s.version, state=s.state)
        if not isinstance(s, str):
            raise TypeError(f"Expected str or Package, got {type(s).__name__!r}")
        return cls.from_string(s, state=coerced)

    @classmethod
    def from_string(cls, s: str, state: State = State.PRESENT) -> "Package":
        if "=" in s:
            name, version = s.split("=", 1)
            return cls(name=name, version=version, state=state)
        return cls(name=s, version="", state=state)

    def __str__(self) -> str:
        return self.name  # pacman does not support name=version pinning


@dataclass
class Result:
    name: str
    changed: bool
    version: str = ""

    @classmethod
    def from_package(cls, package: "Package", changed: bool) -> "Result":
        return cls(name=package.name, version=package.version, changed=changed)


class Backend:
    @staticmethod
    def pacman(*args: str) -> tuple[int, str, str]:
        logging.debug("calling pacman with args: %s", args)
        result = process(
            "pacman",
            "--noconfirm",
            *args,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def query(name: str) -> tuple[bool, str]:
        """Return (installed, version) for a package."""
        rc, stdout, _ = Backend.pacman("-Q", name)
        if rc != 0:
            return False, ""
        parts = stdout.strip().split()
        return True, (parts[1] if len(parts) >= 2 else "")

    @staticmethod
    def parse_upgradable(stdout: str) -> list[str]:
        """Parse package names from 'pacman -Qu' output."""
        return [line.split()[0] for line in stdout.splitlines() if line.split()]


class Pacman(Tool):
    """Manage Arch Linux packages via pacman. Requires root on the remote host."""

    @staticmethod
    def update(ttl: int | float = -1) -> bool:
        """Synchronise the package database (``pacman -Sy``), with optional TTL-based skipping.

        Args:
            ttl: Minimum age in seconds of the last sync before re-running.
                ``-1`` (default) always synchronises.

        Returns:
            ``True`` if ``pacman -Sy`` was executed, ``False`` if the database
            is fresher than *ttl* seconds.

        Raises:
            RuntimeError: If ``pacman -Sy`` exits with a non-zero status.
        """
        stamp = Path("/var/lib/pacman/.rmote-update")
        if ttl >= 0:
            try:
                age = time.time() - stamp.stat().st_mtime
            except FileNotFoundError:
                age = float("inf")
            if age < ttl:
                return False
        rc, _, err = Backend.pacman("-Sy")
        if rc != 0:
            raise RuntimeError(f"pacman -Sy failed:\n{err}")
        stamp.touch()
        return True

    @staticmethod
    def package(package: str | Package, state: State | int = State.PRESENT) -> Result:
        """Install, remove, or upgrade a single package.

        Args:
            package: Package name or a :class:`Package` instance. Version
                pinning (``name=version``) is parsed but ignored by pacman.
            state: Desired state - :attr:`State.PRESENT` (install if absent),
                :attr:`State.ABSENT` (remove if installed), or
                :attr:`State.LATEST` (install or upgrade).

        Returns:
            :class:`Result` with the package name, installed version, and
            whether the system was changed.

        Raises:
            RuntimeError: If the underlying ``pacman`` invocation fails.
        """
        package = Package.parse(package, state=state)
        installed, version = Backend.query(package.name)

        if state == State.PRESENT:
            if installed:
                return Result(name=package.name, version=version, changed=False)
            rc, _, err = Backend.pacman("-S", package.name)
            if rc != 0:
                raise RuntimeError(f"pacman -S {package.name!r} failed:\n{err}")
            _, version = Backend.query(package.name)
            return Result(name=package.name, version=version, changed=True)

        if state == State.ABSENT:
            if not installed:
                return Result(name=package.name, version="", changed=False)
            rc, _, err = Backend.pacman("-R", package.name)
            if rc != 0:
                raise RuntimeError(f"pacman -R {package.name!r} failed:\n{err}")
            return Result(name=package.name, version="", changed=True)

        if state == State.LATEST:
            if not installed:
                rc, _, err = Backend.pacman("-S", package.name)
                if rc != 0:
                    raise RuntimeError(f"pacman -S {package.name!r} failed:\n{err}")
                _, version = Backend.query(package.name)
                return Result(name=package.name, version=version, changed=True)
            _, stdout, _ = Backend.pacman("-Qu")
            if package.name not in Backend.parse_upgradable(stdout):
                return Result(name=package.name, version=version, changed=False)
            rc, _, err = Backend.pacman("-S", package.name)
            if rc != 0:
                raise RuntimeError(f"pacman -S {package.name!r} failed:\n{err}")
            _, version = Backend.query(package.name)
            return Result(name=package.name, version=version, changed=True)

        raise ValueError(f"Unknown state: {state!r}")

    @classmethod
    def converge(cls, *packages: str | Package) -> list[Result]:
        """Ensure all *packages* are present.

        Args:
            *packages: Package names or :class:`Package` instances.

        Returns:
            List of :class:`Result` objects, one per package, in the same
            order as the input.
        """
        return [cls.package(pkg) for pkg in packages]
