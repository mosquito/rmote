import difflib
import re
from enum import IntEnum
from pathlib import Path

from rmote.protocol import Tool


class LineInFileMatch(IntEnum):
    """Controls how regexp matches are replaced by :meth:`FileSystem.line_in_file`.

    Attributes:
        FIRST: Replace only the first line that matches the regexp.
        ALL: Replace every line that matches the regexp.
    """

    FIRST = 0
    ALL = 1


class FileSystem(Tool):
    """Remote filesystem operations - read, glob, and idempotent line-in-file."""

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
        """Read *path* and return its contents decoded as UTF-8.

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
            Sorted list of matching paths as strings.
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
        match the pattern with *line*.  If no line matches, or if *regexp* is not given
        and *line* is not found, *line* is appended.

        Args:
            path: Path to the file to modify.
            line: The desired line content to insert or substitute.
            regexp: A regex pattern to match against existing lines.  When a match is
                found, the matching line(s) are replaced with *line*.
            strip: Compare lines after stripping whitespace when looking for an exact
                match (no *regexp*).  Default ``True``.
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
        else:
            target = line.strip() if strip else line
            for file_line in lines:
                if (file_line.strip() if strip else file_line) == target:
                    return ""  # already present

        if not replaced:
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
