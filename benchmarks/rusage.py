"""Report the CPU this interpreter has used, for measurements across a connection."""

import resource

from rmote.protocol import Tool, inline


class Rusage(Tool):
    @staticmethod
    @inline
    def snapshot() -> tuple[float, float]:
        """Return the user and system seconds of this process."""
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime, usage.ru_stime
