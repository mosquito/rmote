from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from rmote.protocol import Tool, process


class Exec(Tool):
    """Run commands and shell expressions on the remote host."""

    @staticmethod
    def command(
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
        cwd: None | str | Path = None,
        stdin: None | str | bytes = None,
    ) -> CompletedProcess[Any]:
        """Run a command with explicit argument list.

        Args:
            *args: Command and its arguments (e.g. ``"ls"``, ``"-la"``).
            check: Raise :exc:`subprocess.CalledProcessError` on non-zero exit.
            env: Override environment variables for the subprocess.
            cwd: Working directory; defaults to the remote process's cwd.
            stdin: Data written to stdin before the command reads it.

        Returns:
            :class:`subprocess.CompletedProcess` with ``returncode``,
            ``stdout``, and ``stderr``.
        """
        return process(*args, check=check, env=env, cwd=cwd, stdin=stdin)

    @staticmethod
    def shell(
        expression: str,
        check: bool = True,
        env: dict[str, str] | None = None,
        cwd: None | str | Path = None,
        stdin: None | str | bytes = None,
    ) -> CompletedProcess[Any]:
        """Run *expression* through the remote shell (``/bin/sh -c``).

        Args:
            expression: Shell expression, including pipes, redirects, etc.
            check: Raise :exc:`subprocess.CalledProcessError` on non-zero exit.
            env: Override environment variables for the subprocess.
            cwd: Working directory; defaults to the remote process's cwd.
            stdin: Data written to stdin before the command reads it.

        Returns:
            :class:`subprocess.CompletedProcess` with ``returncode``,
            ``stdout``, and ``stderr``.
        """
        return process(expression, check=check, env=env, cwd=cwd, stdin=stdin, shell=True)
