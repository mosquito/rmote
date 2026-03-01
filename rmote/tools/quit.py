import sys

from rmote.protocol import Tool


class Quit(Tool):
    """Signal the remote process to exit cleanly."""

    @staticmethod
    async def exit(code: int = 0) -> None:
        """Exit the remote process with *code*.

        Args:
            code: Exit status code passed to :func:`sys.exit`. Defaults to 0.
        """
        sys.exit(code)
