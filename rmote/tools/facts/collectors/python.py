"""Facts about the interpreter serving remote RPC."""

import platform
import sys
from dataclasses import dataclass

from rmote.protocol import Tool, inline


@dataclass
class PythonInfo:
    version: str
    implementation: str
    executable: str


class PythonFacts(Tool):
    """Collect the remote interpreter's version, implementation and executable.

    Owns the ``python`` key; this describes the interpreter serving RPC.
    """

    key = "python"
    version = 1
    result_type = PythonInfo

    @staticmethod
    @inline
    def collect() -> PythonInfo:
        """Return typed interpreter facts.

        Every value comes from the interpreter itself, so this never waits and
        runs on the event loop of the side that collects it.
        """
        return PythonInfo(
            version=platform.python_version(),
            implementation=platform.python_implementation(),
            executable=sys.executable,
        )


__tool_package__ = "rmote.tools.facts"
