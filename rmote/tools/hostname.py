import socket
from pathlib import Path

from rmote.protocol import Tool
from rmote.requires import needs_linux

_HOSTNAME_FILE = Path("/etc/hostname")
_HOSTS_FILE = Path("/etc/hosts")
_PROC_HOSTNAME = Path("/proc/sys/kernel/hostname")


class Hostname(Tool):
    """Manage system hostname and ``/etc/hosts`` entries. Requires root for mutating operations.

    ``get`` and ``hosts_entry`` work on any POSIX target. ``set`` writes the
    kernel interface ``/proc/sys/kernel/hostname`` and therefore needs Linux;
    on another system it refuses with NotImplementedError.

    Change the hostname in a disposable container with its own UTS namespace::

        >>> from rmote.tools import FileSystem
        >>> remote = getfixture("debian_tool")
        >>> remote(Hostname.set, "rmote-example")
        True
        >>> remote(Hostname.set, "rmote-example")
        False
        >>> remote(Hostname.get)
        'rmote-example'
        >>> remote(FileSystem.read_str, "/etc/hostname").strip()
        'rmote-example'
        >>> remote(Hostname.hosts_entry, "192.0.2.1", "web")
        True
        >>> remote(Hostname.hosts_entry, "192.0.2.1", "web")
        False
    """

    @staticmethod
    def get() -> str:
        """Return the current running hostname.

        ``socket.gethostname`` reads the live value of the kernel, which is
        what ``/etc/hostname`` configures but does not necessarily hold. It
        answers on every target, so this method needs no Linux.

        Returns:
            The hostname string.
        """
        return socket.gethostname()

    @staticmethod
    def set(name: str) -> bool:
        """Set the system hostname idempotently.

        Applies the change immediately by writing to ``/proc/sys/kernel/hostname``
        and persists it to ``/etc/hostname``.  No external commands are required.

        Args:
            name: The desired hostname.

        Returns:
            ``True`` if the hostname was changed, ``False`` if it was already
            set to *name*.

        Raises:
            NotImplementedError: The target is not Linux, so it has no
                ``/proc/sys/kernel/hostname`` to write.
        """
        needs_linux("Hostname.set", "/proc/sys/kernel/hostname")
        current = _PROC_HOSTNAME.read_text().strip()
        if current == name:
            return False
        _PROC_HOSTNAME.write_text(name + "\n")
        _HOSTNAME_FILE.write_text(name + "\n")
        return True

    @staticmethod
    def hosts_entry(ip: str, *names: str) -> bool:
        """Ensure ``/etc/hosts`` has an entry mapping *ip* to *names*.

        If a line for *ip* already exists with exactly the same hostnames
        (in the same order), this is a no-op.  If it exists with different
        hostnames, the line is replaced in-place.  If no line exists for
        *ip*, one is appended.

        Comment lines and blank lines are left untouched.

        Args:
            ip: The IP address for the entry (IPv4 or IPv6).
            *names: One or more hostnames to associate with *ip*.

        Returns:
            ``True`` if ``/etc/hosts`` was modified.

        Raises:
            ValueError: If no *names* are provided.
        """
        if not names:
            raise ValueError("At least one hostname name is required")

        desired_line = ip + "\t" + " ".join(names)
        original = _HOSTS_FILE.read_text() if _HOSTS_FILE.exists() else ""
        lines = original.splitlines(keepends=True)

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            parts = stripped.split()
            if parts and parts[0] == ip:
                reconstructed = ip + "\t" + " ".join(parts[1:])
                if reconstructed == desired_line:
                    return False
                lines[i] = desired_line + "\n"
                _HOSTS_FILE.write_text("".join(lines))
                return True

        # Not found — append
        sep = "" if original.endswith("\n") or not original else "\n"
        _HOSTS_FILE.write_text(original + sep + desired_line + "\n")
        return True
