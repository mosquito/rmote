import logging
from dataclasses import dataclass
from enum import IntEnum

from rmote.protocol import Tool, process


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
    def systemctl(*args: str) -> tuple[int, str, str]:
        logging.debug("calling systemctl with args: %s", args)
        result = process(
            "systemctl",
            *args,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    @classmethod
    def is_active(cls, name: str) -> bool:
        rc, _, _ = cls.systemctl("is-active", "--quiet", name)
        return rc == 0

    @classmethod
    def is_enabled(cls, name: str) -> bool:
        rc, _, _ = cls.systemctl("is-enabled", "--quiet", name)
        return rc == 0

    @classmethod
    def start(cls, name: str) -> None:
        rc, _, err = cls.systemctl("start", name)
        if rc != 0:
            raise RuntimeError(f"systemctl start {name!r} failed:\n{err}")

    @classmethod
    def stop(cls, name: str) -> None:
        rc, _, err = cls.systemctl("stop", name)
        if rc != 0:
            raise RuntimeError(f"systemctl stop {name!r} failed:\n{err}")

    @classmethod
    def restart(cls, name: str) -> None:
        rc, _, err = cls.systemctl("restart", name)
        if rc != 0:
            raise RuntimeError(f"systemctl restart {name!r} failed:\n{err}")

    @classmethod
    def reload(cls, name: str) -> None:
        rc, _, err = cls.systemctl("reload", name)
        if rc != 0:
            raise RuntimeError(f"systemctl reload {name!r} failed:\n{err}")

    @classmethod
    def enable(cls, name: str) -> None:
        rc, _, err = cls.systemctl("enable", name)
        if rc != 0:
            raise RuntimeError(f"systemctl enable {name!r} failed:\n{err}")

    @classmethod
    def disable(cls, name: str) -> None:
        rc, _, err = cls.systemctl("disable", name)
        if rc != 0:
            raise RuntimeError(f"systemctl disable {name!r} failed:\n{err}")

    @classmethod
    def daemon_reload(cls) -> None:
        rc, _, err = cls.systemctl("daemon-reload")
        if rc != 0:
            raise RuntimeError(f"systemctl daemon-reload failed:\n{err}")


class Service(Tool):
    """Manage systemd services on the remote host. Requires systemd and root."""

    @staticmethod
    def status(name: str) -> Result:
        """Return current active/enabled status of a service."""
        return Result(
            name=name,
            started=Backend.is_active(name),
            enabled=Backend.is_enabled(name),
            changed=False,
        )

    @staticmethod
    def start(name: str) -> Result:
        """Start a service. No-op if already running."""
        if Backend.is_active(name):
            return Result(name=name, started=True, enabled=Backend.is_enabled(name), changed=False)
        Backend.start(name)
        return Result(name=name, started=True, enabled=Backend.is_enabled(name), changed=True)

    @staticmethod
    def stop(name: str) -> Result:
        """Stop a service. No-op if already stopped."""
        if not Backend.is_active(name):
            return Result(name=name, started=False, enabled=Backend.is_enabled(name), changed=False)
        Backend.stop(name)
        return Result(name=name, started=False, enabled=Backend.is_enabled(name), changed=True)

    @staticmethod
    def restart(name: str) -> Result:
        """Restart a service unconditionally."""
        Backend.restart(name)
        return Result(name=name, started=True, enabled=Backend.is_enabled(name), changed=True)

    @staticmethod
    def reload(name: str) -> Result:
        """Reload a service unconditionally (SIGHUP)."""
        Backend.reload(name)
        return Result(name=name, started=True, enabled=Backend.is_enabled(name), changed=True)

    @staticmethod
    def enable(name: str) -> Result:
        """Enable a service at boot. No-op if already enabled."""
        if Backend.is_enabled(name):
            return Result(name=name, started=Backend.is_active(name), enabled=True, changed=False)
        Backend.enable(name)
        return Result(name=name, started=Backend.is_active(name), enabled=True, changed=True)

    @staticmethod
    def disable(name: str) -> Result:
        """Disable a service at boot. No-op if already disabled."""
        if not Backend.is_enabled(name):
            return Result(name=name, started=Backend.is_active(name), enabled=False, changed=False)
        Backend.disable(name)
        return Result(name=name, started=Backend.is_active(name), enabled=False, changed=True)

    @staticmethod
    def daemon_reload() -> None:
        """Reload systemd manager configuration."""
        Backend.daemon_reload()

    @staticmethod
    def converge(name: str, *, started: bool = True, enabled: bool = True) -> Result:
        """
        Idempotently ensure a service is in the desired state.

        Args:
            name: Service unit name (e.g. "nginx", "nginx.service")
            started: Whether the service should be running
            enabled: Whether the service should start on boot
        """
        changed = False
        is_active = Backend.is_active(name)
        is_enabled = Backend.is_enabled(name)

        if enabled and not is_enabled:
            Backend.enable(name)
            is_enabled = True
            changed = True
        elif not enabled and is_enabled:
            Backend.disable(name)
            is_enabled = False
            changed = True

        if started and not is_active:
            Backend.start(name)
            is_active = True
            changed = True
        elif not started and is_active:
            Backend.stop(name)
            is_active = False
            changed = True

        return Result(name=name, started=is_active, enabled=is_enabled, changed=changed)
