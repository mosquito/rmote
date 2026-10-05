import logging
from dataclasses import dataclass
from enum import IntEnum

from rmote.process import async_process
from rmote.protocol import Tool
from rmote.requires import needs_program


class State(IntEnum):
    STARTED = 0
    STOPPED = 1
    RESTARTED = 2
    RELOADED = 3


@dataclass
class Result:
    name: str
    started: bool
    enabled: bool
    changed: bool


class Backend:
    @staticmethod
    async def systemctl(*args: str) -> tuple[int, str, str]:
        needs_program("Service", "systemctl", "systemd")
        logging.debug("calling systemctl with args: %s", args)
        result = await async_process(
            "systemctl",
            *args,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    @classmethod
    async def is_active(cls, name: str) -> bool:
        rc, _, _ = await cls.systemctl("is-active", "--quiet", name)
        return rc == 0

    @classmethod
    async def is_enabled(cls, name: str) -> bool:
        rc, _, _ = await cls.systemctl("is-enabled", "--quiet", name)
        return rc == 0

    @classmethod
    async def start(cls, name: str) -> None:
        rc, _, err = await cls.systemctl("start", name)
        if rc != 0:
            raise RuntimeError(f"systemctl start {name!r} failed:\n{err}")

    @classmethod
    async def stop(cls, name: str) -> None:
        rc, _, err = await cls.systemctl("stop", name)
        if rc != 0:
            raise RuntimeError(f"systemctl stop {name!r} failed:\n{err}")

    @classmethod
    async def restart(cls, name: str) -> None:
        rc, _, err = await cls.systemctl("restart", name)
        if rc != 0:
            raise RuntimeError(f"systemctl restart {name!r} failed:\n{err}")

    @classmethod
    async def reload(cls, name: str) -> None:
        rc, _, err = await cls.systemctl("reload", name)
        if rc != 0:
            raise RuntimeError(f"systemctl reload {name!r} failed:\n{err}")

    @classmethod
    async def enable(cls, name: str) -> None:
        rc, _, err = await cls.systemctl("enable", name)
        if rc != 0:
            raise RuntimeError(f"systemctl enable {name!r} failed:\n{err}")

    @classmethod
    async def disable(cls, name: str) -> None:
        rc, _, err = await cls.systemctl("disable", name)
        if rc != 0:
            raise RuntimeError(f"systemctl disable {name!r} failed:\n{err}")

    @classmethod
    async def daemon_reload(cls) -> None:
        rc, _, err = await cls.systemctl("daemon-reload")
        if rc != 0:
            raise RuntimeError(f"systemctl daemon-reload failed:\n{err}")


class Service(Tool):
    """Manage systemd services on the remote host. Requires systemd and root.

    Every operation runs ``systemctl``. A target without it refuses with
    NotImplementedError, which names systemd as what provides it.

    Start and enable a real systemd unit in a disposable Debian container::

        >>> from rmote.tools import Exec, FileSystem
        >>> remote = getfixture("debian_tool")
        >>> unit = "[Service]\\nExecStart=/bin/sleep infinity\\n[Install]\\nWantedBy=multi-user.target\\n"
        >>> remote(FileSystem.write, "/etc/systemd/system/example.service", unit)
        True
        >>> _ = remote(Exec.command, "systemctl", "daemon-reload")
        >>> result = remote(Service.converge, "example.service", started=True, enabled=True)
        >>> result.started, result.enabled, result.changed
        (True, True, True)
        >>> remote(Service.converge, "example.service", started=True, enabled=True).changed
        False
        >>> result = remote(Service.converge, "example.service", started=False, enabled=False)
        >>> result.started, result.enabled, result.changed
        (False, False, True)
        >>> remote(Service.converge, "example.service", started=False, enabled=False).changed
        False
    """

    @staticmethod
    async def status(name: str) -> Result:
        """Return current active/enabled status of a service."""
        return Result(
            name=name,
            started=await Backend.is_active(name),
            enabled=await Backend.is_enabled(name),
            changed=False,
        )

    @staticmethod
    async def start(name: str) -> Result:
        """Start a service. No-op if already running."""
        if await Backend.is_active(name):
            return Result(name=name, started=True, enabled=await Backend.is_enabled(name), changed=False)
        await Backend.start(name)
        return Result(name=name, started=True, enabled=await Backend.is_enabled(name), changed=True)

    @staticmethod
    async def stop(name: str) -> Result:
        """Stop a service. No-op if already stopped."""
        if not await Backend.is_active(name):
            return Result(name=name, started=False, enabled=await Backend.is_enabled(name), changed=False)
        await Backend.stop(name)
        return Result(name=name, started=False, enabled=await Backend.is_enabled(name), changed=True)

    @staticmethod
    async def restart(name: str) -> Result:
        """Restart a service unconditionally."""
        await Backend.restart(name)
        return Result(name=name, started=True, enabled=await Backend.is_enabled(name), changed=True)

    @staticmethod
    async def reload(name: str) -> Result:
        """Reload a service unconditionally (SIGHUP)."""
        await Backend.reload(name)
        return Result(name=name, started=True, enabled=await Backend.is_enabled(name), changed=True)

    @staticmethod
    async def enable(name: str) -> Result:
        """Enable a service at boot. No-op if already enabled."""
        if await Backend.is_enabled(name):
            return Result(name=name, started=await Backend.is_active(name), enabled=True, changed=False)
        await Backend.enable(name)
        return Result(name=name, started=await Backend.is_active(name), enabled=True, changed=True)

    @staticmethod
    async def disable(name: str) -> Result:
        """Disable a service at boot. No-op if already disabled."""
        if not await Backend.is_enabled(name):
            return Result(name=name, started=await Backend.is_active(name), enabled=False, changed=False)
        await Backend.disable(name)
        return Result(name=name, started=await Backend.is_active(name), enabled=False, changed=True)

    @staticmethod
    async def daemon_reload() -> None:
        """Reload systemd manager configuration."""
        await Backend.daemon_reload()

    @staticmethod
    async def converge(name: str, *, started: bool = True, enabled: bool = True) -> Result:
        """
        Idempotently ensure a service is in the desired state.

        Args:
            name: Service unit name (e.g. "nginx", "nginx.service")
            started: Whether the service should be running
            enabled: Whether the service should start on boot
        """
        changed = False
        is_active = await Backend.is_active(name)
        is_enabled = await Backend.is_enabled(name)

        if enabled and not is_enabled:
            await Backend.enable(name)
            is_enabled = True
            changed = True
        elif not enabled and is_enabled:
            await Backend.disable(name)
            is_enabled = False
            changed = True

        if started and not is_active:
            await Backend.start(name)
            is_active = True
            changed = True
        elif not started and is_active:
            await Backend.stop(name)
            is_active = False
            changed = True

        return Result(name=name, started=is_active, enabled=is_enabled, changed=changed)
