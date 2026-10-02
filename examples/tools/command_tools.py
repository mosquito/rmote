import sys

from rmote.protocol import Tool, process


class Commands(Tool):
    @staticmethod
    def python_version() -> str:
        result = process(sys.executable, "--version", capture_output=True, text=True, check=True)
        return result.stdout.strip()
