import sys

from rmote.protocol import Tool


class Quit(Tool):
    """Signal the remote process to exit cleanly.

    Exit a disposable child process rather than the documentation runner::

        >>> import subprocess
        >>> result = subprocess.run(
        ...     [sys.executable, "-c", "import asyncio; from rmote.tools import Quit; asyncio.run(Quit.exit(7))"],
        ...     capture_output=True,
        ... )
        >>> result.returncode
        7
    """

    @staticmethod
    async def exit(code: int = 0) -> None:
        """Exit the remote process with *code*.

        Args:
            code: Exit status code passed to :func:`sys.exit`. Defaults to 0.
        """
        sys.exit(code)
