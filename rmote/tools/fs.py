import difflib
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from rmote.protocol import Tool


@dataclass(frozen=True, slots=True)
class StatResult:
    path: str
    exists: bool
    size: int = 0
    mode: int = 0
    uid: int = 0
    gid: int = 0
    mtime: float = 0.0
    is_file: bool = False
    is_dir: bool = False
    is_symlink: bool = False
    link_target: str = field(default="")


class LineInFileMatch(IntEnum):
    """Controls how regexp matches are replaced by :meth:`FileSystem.line_in_file`.

    Attributes:
        FIRST: Replace only the first line that matches the regexp.
        ALL: Replace every line that matches the regexp.
    """

    FIRST = 0
    ALL = 1


class FileSystem(Tool):
    """Remote filesystem operations - read, glob, and idempotent line-in-file.

    Example using disposable files (the same methods can be called remotely)::

        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = str(Path(directory) / "app.conf")
        ...     print(FileSystem.write(path, "enabled=no\\n"))
        ...     print(FileSystem.write(path, "enabled=no\\n"))
        ...     diff = FileSystem.line_in_file(path, line="enabled=yes", regexp="^enabled=")
        ...     print(bool(diff), FileSystem.read_str(path).strip())
        ...     print(FileSystem.line_in_file(path, line="enabled=yes", regexp="^enabled="))
        ...     print(FileSystem.stat(path).is_file)
        ...     print(FileSystem.absent(path), FileSystem.absent(path))
        True
        False
        True enabled=yes
        <BLANKLINE>
        True
        True False
    """

    @staticmethod
    def read_bytes(path: str) -> bytes:
        """Read *path* and return its raw contents.

        Args:
            path: Absolute or relative path on the remote filesystem.

        Returns:
            File contents as :class:`bytes`.
        """
        return Path(path).read_bytes()

    @staticmethod
    def read_str(path: str) -> str:
        """Read *path* using the remote Python process's default text encoding.

        Args:
            path: Absolute or relative path on the remote filesystem.

        Returns:
            File contents as :class:`str`.
        """
        return Path(path).read_text()

    @staticmethod
    def glob(path: str | Path, pattern: str) -> list[str]:
        """Return all paths under *path* that match *pattern*.

        Args:
            path: Directory to search in.
            pattern: Glob pattern relative to *path* (e.g. ``"*.conf"``).

        Returns:
            Matching paths as strings, with no guaranteed order.
        """
        return list(map(str, Path(path).glob(pattern)))

    @staticmethod
    def line_in_file(
        path: str,
        *,
        line: str,
        regexp: str | None = None,
        strip: bool = True,
        create: bool = False,
        match: LineInFileMatch = LineInFileMatch.FIRST,
    ) -> str:
        """Ensure *line* is present in the file at *path*, idempotently.

        If *regexp* is given, replace the first (or all, with ``match=ALL``) lines that
        match the pattern with *line*. If no pattern matches, append *line* only
        when it is absent. Without *regexp*, append *line* only when it is absent.

        Args:
            path: Path to the file to modify.
            line: The desired line content to insert or substitute.
            regexp: A regex pattern to match against existing lines.  When a match is
                found, the matching line(s) are replaced with *line*.
            strip: Compare lines after stripping whitespace when looking for an exact
                match, including when *regexp* has no matches. Default ``True``.
            create: Create the file if it does not exist.  Default ``False``.
            match: Whether to replace the :attr:`~LineInFileMatch.FIRST` matching line
                or :attr:`~LineInFileMatch.ALL` matching lines.

        Returns:
            A unified diff string describing the change, or an empty string if the file
            was already in the desired state.

        Raises:
            FileNotFoundError: If *path* does not exist and *create* is ``False``.
        """
        p = Path(path)
        if not p.exists():
            if create:
                p.touch()
            else:
                raise FileNotFoundError(f"File {p} does not exist")

        original = p.read_text()
        lines = original.splitlines()
        replaced = 0

        if regexp is not None:
            pattern = re.compile(regexp)
            for i, file_line in enumerate(lines):
                if pattern.search(file_line):
                    lines[i] = line
                    replaced += 1
                    if match == LineInFileMatch.FIRST:
                        break
        if not replaced:
            target = line.strip() if strip else line
            for file_line in lines:
                if (file_line.strip() if strip else file_line) == target:
                    return ""  # already present

            lines.append(line)

        trailing = "\n" if original.endswith("\n") else ""
        new_content = "\n".join(lines) + trailing

        if new_content == original:
            return ""

        p.write_text(new_content)
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=str(p),
                tofile=str(p),
            )
        )

    @staticmethod
    def write(
        path: str,
        content: str | bytes,
        *,
        mode: int = 0o644,
        owner: str | None = None,
        group: str | None = None,
    ) -> bool:
        """Write *content* to *path* idempotently.

        All of content, mode, owner, and group are compared to the current state.
        Returns ``True`` if any of them differed and the file was updated.

        Args:
            path: Destination path on the remote filesystem.
            content: File content as a string (UTF-8) or bytes.
            mode: File permission bits (default ``0o644``).
            owner: Owner username; ``None`` leaves ownership unchanged.
            group: Group name; ``None`` leaves group unchanged.

        Returns:
            ``True`` if the file was created or updated, ``False`` if already
            in the desired state.
        """
        import grp
        import pwd

        raw = content.encode() if isinstance(content, str) else content
        p = Path(path)

        desired_uid = pwd.getpwnam(owner).pw_uid if owner is not None else None
        desired_gid = grp.getgrnam(group).gr_gid if group is not None else None

        changed = False
        if p.exists():
            st = p.stat()
            current_mode = stat.S_IMODE(st.st_mode)
            content_match = p.read_bytes() == raw
            mode_match = current_mode == mode
            uid_match = desired_uid is None or st.st_uid == desired_uid
            gid_match = desired_gid is None or st.st_gid == desired_gid
            if content_match and mode_match and uid_match and gid_match:
                return False
            changed = True
        else:
            changed = True

        p.write_bytes(raw)
        p.chmod(mode)
        if desired_uid is not None or desired_gid is not None:
            os.chown(p, desired_uid if desired_uid is not None else -1, desired_gid if desired_gid is not None else -1)
        return changed

    @staticmethod
    def directory(
        path: str,
        *,
        mode: int = 0o755,
        owner: str | None = None,
        group: str | None = None,
    ) -> bool:
        """Ensure a directory exists at *path* with the given permissions.

        Creates the directory (and any missing parents) if absent.  If it
        already exists, applies *mode*, *owner*, and *group* if they differ.

        Args:
            path: Target directory path.
            mode: Directory permission bits (default ``0o755``).
            owner: Owner username; ``None`` leaves ownership unchanged.
            group: Group name; ``None`` leaves group unchanged.

        Returns:
            ``True`` if the directory was created or its attributes changed.
        """
        import grp
        import pwd

        p = Path(path)
        desired_uid = pwd.getpwnam(owner).pw_uid if owner is not None else None
        desired_gid = grp.getgrnam(group).gr_gid if group is not None else None

        changed = False
        if not p.exists():
            p.mkdir(parents=True, mode=mode)
            p.chmod(mode)
            changed = True
        else:
            st = p.stat()
            current_mode = stat.S_IMODE(st.st_mode)
            if current_mode != mode:
                p.chmod(mode)
                changed = True
            uid_match = desired_uid is None or st.st_uid == desired_uid
            gid_match = desired_gid is None or st.st_gid == desired_gid
            if not uid_match or not gid_match:
                changed = True

        if desired_uid is not None or desired_gid is not None:
            os.chown(p, desired_uid if desired_uid is not None else -1, desired_gid if desired_gid is not None else -1)
        return changed

    @staticmethod
    def symlink(path: str, target: str) -> bool:
        """Ensure *path* is a symlink pointing to *target*.

        If *path* already exists as the correct symlink, this is a no-op.
        If it exists as a wrong symlink or a regular file, it is replaced.

        Args:
            path: Path where the symlink should be created.
            target: The target the symlink should point to.

        Returns:
            ``True`` if the symlink was created or updated.
        """
        p = Path(path)
        if p.is_symlink():
            if os.readlink(p) == target:
                return False
            p.unlink()
        elif p.exists():
            p.unlink()
        os.symlink(target, p)
        return True

    @staticmethod
    def absent(path: str, *, recursive: bool = False) -> bool:
        """Remove *path* if it exists.

        Args:
            path: Path to remove.
            recursive: If ``True``, remove directories and their contents
                recursively (equivalent to ``rm -rf``).  If ``False``
                (default), non-empty directories raise :exc:`OSError`.

        Returns:
            ``True`` if *path* existed and was removed, ``False`` if it was
            already absent.
        """
        p = Path(path)
        if not p.exists() and not p.is_symlink():
            return False
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif recursive:
            shutil.rmtree(p)
        else:
            p.rmdir()
        return True

    @staticmethod
    def stat(path: str) -> StatResult:
        """Return metadata for *path* without following symlinks.

        Args:
            path: Path to inspect.

        Returns:
            :class:`StatResult` with file metadata.  If *path* does not
            exist, returns a :class:`StatResult` with ``exists=False`` and
            all numeric fields set to zero.
        """
        p = Path(path)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return StatResult(path=path, exists=False)
        is_link = p.is_symlink()
        return StatResult(
            path=path,
            exists=True,
            size=st.st_size,
            mode=stat.S_IMODE(st.st_mode),
            uid=st.st_uid,
            gid=st.st_gid,
            mtime=st.st_mtime,
            is_file=p.is_file() and not is_link,
            is_dir=p.is_dir() and not is_link,
            is_symlink=is_link,
            link_target=os.readlink(p) if is_link else "",
        )
