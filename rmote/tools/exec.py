from pathlib import Path
from subprocess import CompletedProcess

from rmote.protocol import Tool, process


class Exec(Tool):
    """Run commands and shell expressions on the remote host.

    Capture binary output and inspect nonzero exit codes without a shell::

        >>> import sys
        >>> result = Exec.command(sys.executable, "-c", "print('hello')", capture_output=True)
        >>> result.returncode, result.stdout
        (0, b'hello\\n')
        >>> Exec.command(sys.executable, "-c", "raise SystemExit(3)", check=False).returncode
        3
        >>> Exec.shell("printf '%s' hello", capture_output=True).stdout
        b'hello'
    """

    @staticmethod
    def command(
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
        cwd: None | str | Path = None,
        stdin: None | str | bytes = None,
        capture_output: bool = False,
    ) -> CompletedProcess[bytes | None]:
        """Run a command with explicit argument list.

        Args:
            *args: Command and its arguments (e.g. ``"ls"``, ``"-la"``).
            check: Raise :exc:`subprocess.CalledProcessError` on non-zero exit,
                with stdout and stderr when capture_output is enabled.
            env: Override environment variables for the subprocess.
            cwd: Working directory; defaults to the remote process's cwd.
            stdin: Data written to stdin before the command reads it.
            capture_output: Capture stdout and stderr as bytes. By default,
                discard both streams on the remote host.

        Returns:
            :class:`subprocess.CompletedProcess` with ``returncode``,
            ``stdout``, and ``stderr``. Output fields contain bytes when captured,
            or None when capture_output is False.
        """
        return process(*args, capture_output=capture_output, check=check, env=env, cwd=cwd, stdin=stdin)

    @staticmethod
    def shell(
        expression: str,
        check: bool = True,
        env: dict[str, str] | None = None,
        cwd: None | str | Path = None,
        stdin: None | str | bytes = None,
        capture_output: bool = False,
    ) -> CompletedProcess[bytes | None]:
        """Run *expression* through the remote shell (``/bin/sh -c``).

        Args:
            expression: Shell expression, including pipes, redirects, etc.
            check: Raise :exc:`subprocess.CalledProcessError` on non-zero exit,
                with stdout and stderr when capture_output is enabled.
            env: Override environment variables for the subprocess.
            cwd: Working directory; defaults to the remote process's cwd.
            stdin: Data written to stdin before the command reads it.
            capture_output: Capture stdout and stderr as bytes. By default,
                discard both streams on the remote host.

        Returns:
            :class:`subprocess.CompletedProcess` with ``returncode``,
            ``stdout``, and ``stderr``. Output fields contain bytes when captured,
            or None when capture_output is False.
        """
        return process(
            expression, capture_output=capture_output, check=check, env=env, cwd=cwd, stdin=stdin, shell=True
        )
