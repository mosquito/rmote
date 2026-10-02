import shutil
from dataclasses import dataclass

from rmote.protocol import Tool


@dataclass
class DiskSpace:
    total: int
    used: int
    free: int


class DiskInfo(Tool):
    @staticmethod
    def usage(path: str) -> DiskSpace:
        usage = shutil.disk_usage(path)
        return DiskSpace(total=usage.total, used=usage.used, free=usage.free)
