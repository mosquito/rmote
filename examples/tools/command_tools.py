import sys

from rmote.process import async_process
from rmote.protocol import Tool


class Commands(Tool):
    @staticmethod
    async def python_version() -> str:
        result = await async_process(sys.executable, "--version", capture_output=True, text=True, check=True)
        assert isinstance(result.stdout, str)
        return result.stdout.strip()
