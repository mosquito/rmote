"""Read-only systemd manager and unit facts."""

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rmote.process import async_process
from rmote.protocol import Tool


@dataclass
class SystemdInfo:
    available: bool
    manager_available: bool = False
    version: str | None = None
    manager: dict[str, str] | None = None
    units: dict[str, dict[str, Any]] | None = None
    unit_files: dict[str, dict[str, Any]] | None = None


@dataclass
class TimesyncInfo:
    available: bool
    manager_available: bool = False
    clock: dict[str, str] | None = None
    service: dict[str, str] | None = None
    timesync: dict[str, str] | None = None


@dataclass
class ResolvedInfo:
    available: bool
    manager_available: bool = False
    service: dict[str, str] | None = None
    status: list[dict[str, Any]] | None = None


class SystemdCommand:
    """Shared noninteractive command and property parsing for systemd collectors."""

    @staticmethod
    async def run(executable: str, *arguments: str) -> str:
        """Run a query with a 30-second timeout and stable locale."""
        result = await async_process(
            executable,
            "--no-pager",
            *arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
        )
        return cast(str, result.stdout)

    @staticmethod
    def properties(output: str) -> dict[str, str]:
        """Parse machine-readable key=value properties, preserving empty values."""
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    @staticmethod
    async def service(executable: str, unit: str) -> dict[str, str]:
        """Inspect a daemon without starting it or prompting for authentication."""
        return SystemdCommand.properties(
            await SystemdCommand.run(
                executable,
                "--no-ask-password",
                "show",
                unit,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
            )
        )


class SystemdFacts(Tool):
    """Collect system manager properties, loaded units and installed unit files.

    Owns ``systemd``. ``available`` reports whether systemctl is installed;
    ``manager_available`` reports the booted system manager via
    ``/run/systemd/system``. When installed, ``version`` contains the complete
    systemctl version output and ``unit_files`` maps unit names to their
    installation state and preset (where provided by systemd).

    With a booted manager, ``manager`` contains all ``systemctl show`` properties
    as strings, and ``units`` maps names to list-units records including load,
    active and sub states and description. This includes services, sockets,
    timers, mounts and other unit types, including inactive loaded units.
    Installed but unloaded units remain visible through ``unit_files``.

    Unavailable sections are None, not empty successful queries. A container
    with systemctl but no manager can still report its installed unit files.
    Queries use the system manager, never start/stop units, and require systemd
    with JSON output for list-units/list-unit-files. Unsupported JSON, timeouts,
    permission and bus errors propagate. Each command has a 30-second limit.
    """

    key = "systemd"
    version = 1
    result_type = SystemdInfo

    @staticmethod
    async def systemctl(executable: str, *arguments: str) -> str:
        """Run a noninteractive read-only query with a 30-second timeout."""
        return await SystemdCommand.run(executable, "--no-ask-password", *arguments)

    @staticmethod
    def unit_records(output: str, name_key: str) -> dict[str, dict[str, Any]]:
        """Index a systemctl JSON array by its unit name, retaining all fields."""
        value = json.loads(output)
        if not isinstance(value, list):
            raise ValueError("Expected a JSON array from systemctl")
        result = {}
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get(name_key), str):
                raise ValueError(f"Expected {name_key} in systemctl record")
            result[item[name_key]] = item
        return result

    @staticmethod
    async def collect() -> SystemdInfo:
        """Return the system manager and unit inventory visible to the RPC user."""
        executable = shutil.which("systemctl")
        result: SystemdInfo = SystemdInfo(
            available=executable is not None,
            manager_available=False,
            version=None,
            manager=None,
            units=None,
            unit_files=None,
        )
        if executable is None:
            return result
        result.version = (await SystemdFacts.systemctl(executable, "--version")).strip()
        result.unit_files = SystemdFacts.unit_records(
            (await SystemdFacts.systemctl(executable, "list-unit-files", "--output=json")), "unit_file"
        )
        if Path("/run/systemd/system").is_dir():
            result.manager_available = True
            result.manager = SystemdCommand.properties(await SystemdFacts.systemctl(executable, "show", "--all"))
            result.units = SystemdFacts.unit_records(
                (await SystemdFacts.systemctl(executable, "list-units", "--all", "--output=json")), "unit"
            )
        return result


class SystemdTimesyncFacts(Tool):
    """Collect clock/NTP state and systemd-timesyncd properties independently.

    Owns ``systemd_timesync``. Requires timedatectl and systemctl, checked before
    execution. ``clock`` is ``timedatectl show --all``: timezone, RTC mode,
    whether NTP is enabled and the clock is synchronized. ``service`` contains
    timesyncd load/active/sub state; ``timesync`` contains show-timesync
    properties such as configured/current servers, poll intervals, frequency
    and the latest NTP message. Property values remain strings, matching the
    machine-readable CLI interface.

    ``available`` reports both binaries; ``manager_available`` reports a booted
    system manager. Missing sources are None. If timesyncd is stopped, missing,
    or replaced by another NTP service, clock properties remain available but
    ``timesync`` is None. Collection does not start timesyncd. Errors querying
    an active daemon propagate. Every command has a 30-second timeout.
    """

    key = "systemd_timesync"
    version = 1
    result_type = TimesyncInfo

    @staticmethod
    async def collect() -> TimesyncInfo:
        """Return clock state and, when running, timesyncd's own observations."""
        executable = shutil.which("timedatectl")
        control = shutil.which("systemctl")
        result: TimesyncInfo = TimesyncInfo(
            available=executable is not None and control is not None,
            manager_available=False,
            clock=None,
            service=None,
            timesync=None,
        )
        if executable is None or control is None or not Path("/run/systemd/system").is_dir():
            return result
        result.manager_available = True
        result.clock = SystemdCommand.properties(
            await SystemdCommand.run(executable, "--no-ask-password", "show", "--all")
        )
        service = await SystemdCommand.service(control, "systemd-timesyncd.service")
        result.service = service
        if service.get("ActiveState") in ("active", "reloading"):
            result.timesync = SystemdCommand.properties(
                await SystemdCommand.run(executable, "--no-ask-password", "show-timesync", "--all")
            )
        return result


class SystemdResolvedFacts(Tool):
    """Collect systemd-resolved's global and per-link DNS configuration.

    Owns ``systemd_resolved``. Requires resolvectl and systemctl. ``service``
    reports resolved load/active/sub state. When active, ``status`` is the JSON
    array from ``resolvectl --json=short status``, preserving global and per-link
    records, DNS servers, routing/search domains, default-route selection,
    DNSSEC, DNS-over-TLS, LLMNR and mDNS fields where reported by the daemon.

    Missing binaries or an unbooted manager are indicated by ``available`` and
    ``manager_available``; unavailable sections are None. A stopped/missing
    resolved is not started. Queries never resolve names or flush caches.
    Before systemd 259, resolvectl cannot report JSON status, so ``status`` is
    None. Errors from supported queries propagate. Privileged monitoring and
    statistics are not collected. Each query has a 30-second timeout.
    """

    key = "systemd_resolved"
    version = 1
    result_type = ResolvedInfo

    @staticmethod
    async def collect() -> ResolvedInfo:
        """Read DNS configuration only when systemd-resolved is already active."""
        executable = shutil.which("resolvectl")
        control = shutil.which("systemctl")
        result: ResolvedInfo = ResolvedInfo(
            available=executable is not None and control is not None,
            manager_available=False,
            service=None,
            status=None,
        )
        if executable is None or control is None or not Path("/run/systemd/system").is_dir():
            return result
        result.manager_available = True
        service = await SystemdCommand.service(control, "systemd-resolved.service")
        result.service = service
        if service.get("ActiveState") in ("active", "reloading"):
            version = await SystemdCommand.run(executable, "--version")
            match = re.match(r"systemd (\d+)", version)
            if match is None:
                raise ValueError("Expected a systemd version from resolvectl")
            if int(match[1]) < 259:
                return result
            status = json.loads(await SystemdCommand.run(executable, "--json=short", "status"))
            if not isinstance(status, list) or any(not isinstance(item, dict) for item in status):
                raise ValueError("Expected a JSON array of objects from resolvectl status")
            result.status = status
        return result


__tool_package__ = "rmote.tools.facts"
