from rmote.protocol import Tool, process


class Cache(Tool):
    @staticmethod
    def stats(port: int) -> dict[str, str]:
        result = process(
            "redis-cli",
            "-p",
            str(port),
            "INFO",
            "stats",
            capture_output=True,
            check=True,
            text=True,
        )
        stats = {}
        for line in result.stdout.splitlines():
            if line and not line.startswith("#"):
                key, separator, value = line.partition(":")
                if separator:
                    stats[key] = value
        return stats
