from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from rmote.protocol import Tool

_SOURCES_DIR = Path("/etc/apt/sources.list.d")
_KEYRINGS_DIR = Path("/etc/apt/keyrings")


@dataclass
class Result:
    name: str
    changed: bool


class AptRepository(Tool):
    """Manage APT repository sources and GPG keys on Debian/Ubuntu.

    Repository sources are written as DEB822 ``.sources`` files under
    ``/etc/apt/sources.list.d/``. GPG keyrings are stored in
    ``/etc/apt/keyrings/``. All operations are idempotent and require root.
    """

    @staticmethod
    def key(name: str, data: bytes) -> Result:
        """Install a GPG signing key to ``/etc/apt/keyrings/{name}``.

        The operation is idempotent — if the file already contains identical bytes it is
        left untouched.

        Args:
            name: Filename to write the key under (e.g. ``"docker.gpg"``).
            data: Raw key bytes (binary DER or ASCII-armoured GPG key).

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
        types: Iterable[str] = ("deb",),
        uris: Iterable[str],
        suites: Iterable[str],
        components: Iterable[str],
        signed_by: str = "",
    ) -> Result:
        """Ensure a DEB822 ``.sources`` file is configured for *name*.

        Writes ``/etc/apt/sources.list.d/{name}.sources``.  The operation is idempotent
        — if the file already contains the expected content it is left untouched.

        Args:
            name: Repository label used as the filename stem.
            types: Package types — typically ``("deb",)`` for binary packages or
                ``("deb-src",)`` for source packages.
            uris: Repository base URIs (e.g. ``("https://download.docker.com/linux/ubuntu",)``).
            suites: Distribution suites (e.g. ``("noble",)``).
            components: Repository components (e.g. ``("stable",)``).
            signed_by: Absolute path to the GPG keyring that signs this repository.

        Returns:
            :class:`Result` indicating whether the file was created or updated.
        """
        lines = [
            f"Types: {' '.join(types)}",
            f"URIs: {' '.join(uris)}",
            f"Suites: {' '.join(suites)}",
            f"Components: {' '.join(components)}",
        ]
        if signed_by:
            lines.append(f"Signed-By: {signed_by}")
        content = "\n".join(lines) + "\n"

        _SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        path = _SOURCES_DIR / f"{name}.sources"
        if path.exists() and path.read_text() == content:
            return Result(name=name, changed=False)
        path.write_text(content)
        return Result(name=name, changed=True)

    @staticmethod
    def absent(name: str) -> Result:
        """Remove the DEB822 ``.sources`` file for *name*, if it exists.

        Args:
            name: Repository label (the ``{name}.sources`` filename stem).

        Returns:
            :class:`Result` indicating whether the file was removed.
        """
        p = _SOURCES_DIR / f"{name}.sources"
        if p.exists():
            p.unlink()
            return Result(name=name, changed=True)
        return Result(name=name, changed=False)
