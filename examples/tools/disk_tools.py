import shutil

from rmote.protocol import Tool


class Disk(Tool):
    @staticmethod
    def free_bytes(path: str) -> int:
        return shutil.disk_usage(path).free
