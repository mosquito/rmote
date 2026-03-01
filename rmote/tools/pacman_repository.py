import re
from dataclasses import dataclass
from pathlib import Path

from rmote.protocol import Tool

_PACMAN_CONF = Path("/etc/pacman.conf")
_KEYRINGS_DIR = Path("/etc/pacman.d")


@dataclass
class Result:
    name: str
    changed: bool


def _build_section(name: str, servers: list[str], sig_level: str) -> str:
    lines = [f"[{name}]"]
    if sig_level:
        lines.append(f"SigLevel = {sig_level}")
    for server in servers:
        lines.append(f"Server = {server}")
    return "\n".join(lines) + "\n"


def _section_bounds(content: str, name: str) -> tuple[int, int] | None:
    """Return (start, end) byte offsets of the [name] section, or None."""
    m = re.search(r"^\[" + re.escape(name) + r"\][ \t]*$", content, re.MULTILINE)
    if not m:
        return None
    next_m = re.search(r"^\[", content[m.end() :], re.MULTILINE)
    end = m.end() + next_m.start() if next_m else len(content)
    return m.start(), end


class PacmanRepository(Tool):
    """Manage Arch Linux repository sections and GPG keys.

    Repository sections are read from and written to ``/etc/pacman.conf``.
    GPG key files are stored in ``/etc/pacman.d/``. All operations are
    idempotent and require root.
    """

    @staticmethod
    def key(name: str, data: bytes) -> Result:
        """Write a GPG key to ``/etc/pacman.d/{name}``.

        Idempotent — the file is left untouched if it already contains identical bytes.

        Args:
            name: Filename to write the key under (e.g. ``"blackarch.gpg"``).
            data: Raw key bytes.

        Returns:
            :class:`Result` indicating whether the key was written.
        """
        _KEYRINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = _KEYRINGS_DIR / name
        if path.exists() and path.read_bytes() == data:
            return Result(name=name, changed=False)
        path.write_bytes(data)
        return Result(name=name, changed=True)

    @staticmethod
    def present(
        name: str,
        *,
        servers: list[str],
        sig_level: str = "Optional TrustAll",
    ) -> Result:
        """Ensure a repository section is present in ``/etc/pacman.conf``.

        If the section already exists with the same content it is left unchanged.  If it
        exists with different content it is updated in-place.  Otherwise it is appended
        at the end of the file.

        Args:
            name: Repository section name (e.g. ``"blackarch"``).
            servers: One or more server URLs for the repository.
            sig_level: pacman ``SigLevel`` value.  Defaults to
                ``"Optional TrustAll"``.

        Returns:
            :class:`Result` indicating whether ``pacman.conf`` was modified.
        """
        section = _build_section(name, servers, sig_level)
        content = _PACMAN_CONF.read_text()
        bounds = _section_bounds(content, name)

        if bounds is not None:
            start, end = bounds
            if content[start:end] == section:
                return Result(name=name, changed=False)
            new_content = content[:start] + section + content[end:]
        else:
            new_content = content.rstrip("\n") + "\n\n" + section

        _PACMAN_CONF.write_text(new_content)
        return Result(name=name, changed=True)

    @staticmethod
    def absent(name: str) -> Result:
        """Remove the repository section *name* from ``/etc/pacman.conf``.

        Args:
            name: Repository section name to remove.

        Returns:
            :class:`Result` indicating whether ``pacman.conf`` was modified.
        """
        content = _PACMAN_CONF.read_text()
        bounds = _section_bounds(content, name)
        if bounds is None:
            return Result(name=name, changed=False)
        start, end = bounds
        new_content = re.sub(r"\n{3,}", "\n\n", content[:start] + content[end:])
        _PACMAN_CONF.write_text(new_content)
        return Result(name=name, changed=True)
