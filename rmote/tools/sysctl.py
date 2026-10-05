from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rmote.protocol import Tool
from rmote.requires import needs_linux


@dataclass
class Result:
    key: str
    value: str
    changed: bool


class Backend:
    @staticmethod
    def read_conf(conf_file: Path) -> dict[str, str]:
        """Parse ``key = value`` lines from *conf_file*.

        Lines starting with ``#`` and blank lines are ignored.
        """
        if not conf_file.exists():
            return {}
        result: dict[str, str] = {}
        for line in conf_file.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, sep, value = stripped.partition("=")
            if sep:
                result[key.strip()] = value.strip()
        return result

    @staticmethod
    def write_conf(conf_file: Path, data: dict[str, str]) -> None:
        """Write *data* as ``key = value`` lines to *conf_file*."""
        conf_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k} = {v}\n" for k, v in sorted(data.items())]
        conf_file.write_text("".join(lines))

    @staticmethod
    def proc_path(key: str) -> Path:
        """Map ``net.ipv4.ip_forward`` → ``/proc/sys/net/ipv4/ip_forward``."""
        return Path("/proc/sys") / key.replace(".", "/")

    @classmethod
    def get_runtime(cls, key: str) -> str:
        """Read the current runtime value of *key* from ``/proc/sys/``.

        Raises:
            NotImplementedError: The target is not Linux and has no such
                interface.
            FileNotFoundError: The target is Linux and has no such key.
        """
        needs_linux("Sysctl", "/proc/sys")
        return cls.proc_path(key).read_text().strip()

    @classmethod
    def apply(cls, key: str, value: str) -> None:
        """Apply *key=value* immediately by writing to ``/proc/sys/``.

        Raises:
            NotImplementedError: The target is not Linux and has no such
                interface.
        """
        needs_linux("Sysctl", "/proc/sys")
        cls.proc_path(key).write_text(value + "\n")


class Sysctl(Tool):
    """Manage kernel parameters via sysctl.

    Runtime values are applied immediately via ``/proc/sys/``.  Changes are
    persisted to a file under :attr:`CONF_DIR` so they survive reboots.
    Requires root.

    The runtime value needs Linux: ``get``, ``present`` and ``converge``
    refuse with NotImplementedError on another system. ``absent`` only edits
    the persistent file, which is an ordinary file on any target.

    Examples::

        # default file: /etc/sysctl.d/99-rmote.conf
        Sysctl.present("net.ipv4.ip_forward", "1")

        # custom file name inside CONF_DIR
        Sysctl.present("net.ipv4.ip_forward", "1", name="50-network.conf")
        Sysctl.converge({"vm.swappiness": "10"}, name="50-network.conf")

        # custom directory via subclass
        class MySysctl(Sysctl):
            CONF_DIR = Path("/etc/sysctl.d")


    Change a namespaced network parameter in a disposable container. The example
    checks the runtime value, persistent configuration and repeated application::

        >>> from rmote.tools import FileSystem
        >>> remote = getfixture("debian_tool")
        >>> value = "0" if remote(Sysctl.get, "net.ipv4.ip_forward") == "1" else "1"
        >>> remote(Sysctl.present, "net.ipv4.ip_forward", value).changed
        True
        >>> remote(Sysctl.get, "net.ipv4.ip_forward") == value
        True
        >>> "net.ipv4.ip_forward = " + value in remote(FileSystem.read_str, "/etc/sysctl.d/99-rmote.conf")
        True
        >>> remote(Sysctl.present, "net.ipv4.ip_forward", value).changed
        False
        >>> remote(Sysctl.absent, "net.ipv4.ip_forward")
        True
        >>> remote(Sysctl.absent, "net.ipv4.ip_forward")
        False
    """

    CONF_DIR: ClassVar[Path] = Path("/etc/sysctl.d")

    @classmethod
    def get(cls, key: str) -> str:
        """Return the current runtime value of *key*.

        Args:
            key: Sysctl key (e.g. ``"net.ipv4.ip_forward"``).

        Returns:
            Current value as a string.
        """
        return Backend.get_runtime(key)

    @classmethod
    def present(cls, key: str, value: str, *, name: str = "99-rmote.conf") -> Result:
        """Ensure *key* has *value* at runtime and in ``CONF_DIR/name``.

        Reads the current runtime value and the persisted conf.  Only makes
        changes if the runtime value or the conf entry differ from *value*.

        Args:
            key: Sysctl key (e.g. ``"net.ipv4.ip_forward"``).
            value: Desired value (e.g. ``"1"``).
            name: Filename inside :attr:`CONF_DIR` (default ``"99-rmote.conf"``).

        Returns:
            :class:`Result` with the key, applied value, and whether anything
            changed.
        """
        conf_file = cls.CONF_DIR / name
        conf = Backend.read_conf(conf_file)
        try:
            runtime = Backend.get_runtime(key)
        except FileNotFoundError:
            runtime = None

        if (runtime is None or runtime == value) and conf.get(key) == value:
            return Result(key=key, value=value, changed=False)

        if runtime is not None and runtime != value:
            Backend.apply(key, value)
        if conf.get(key) != value:
            conf[key] = value
            Backend.write_conf(conf_file, conf)
        return Result(key=key, value=value, changed=True)

    @classmethod
    def absent(cls, key: str, *, name: str = "99-rmote.conf") -> bool:
        """Remove *key* from ``CONF_DIR/name``.

        Does **not** reset the runtime value — the current kernel value is
        left untouched.

        Args:
            key: Sysctl key to remove from persistent config.
            name: Filename inside :attr:`CONF_DIR` (default ``"99-rmote.conf"``).

        Returns:
            ``True`` if the key was present and removed, ``False`` if it was
            already absent.
        """
        conf_file = cls.CONF_DIR / name
        conf = Backend.read_conf(conf_file)
        if key not in conf:
            return False
        del conf[key]
        Backend.write_conf(conf_file, conf)
        return True

    @classmethod
    def converge(cls, params: dict[str, str], *, name: str = "99-rmote.conf") -> list[Result]:
        """Ensure multiple sysctl keys have the desired values.

        Reads the conf file once, applies all changes, then writes the conf
        file once — more efficient than calling :meth:`present` in a loop.

        Args:
            params: Mapping of sysctl key → desired value,
                e.g. ``{"net.ipv4.ip_forward": "1", "vm.swappiness": "10"}``.
            name: Filename inside :attr:`CONF_DIR` (default ``"99-rmote.conf"``).

        Returns:
            List of :class:`Result` objects, one per key.
        """
        conf_file = cls.CONF_DIR / name
        conf = Backend.read_conf(conf_file)
        results: list[Result] = []
        conf_changed = False

        for key, value in params.items():
            try:
                runtime = Backend.get_runtime(key)
            except FileNotFoundError:
                runtime = None

            changed = False
            if runtime is not None and runtime != value:
                Backend.apply(key, value)
                changed = True
            if conf.get(key) != value:
                conf[key] = value
                conf_changed = True
                changed = True
            results.append(Result(key=key, value=value, changed=changed))

        if conf_changed:
            Backend.write_conf(conf_file, conf)
        return results
