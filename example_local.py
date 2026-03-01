import asyncio
import asyncio.subprocess
import logging
import sys

from rmote import protocol
from rmote.tools import Logger
from rmote.tools.fs import FileSystem


async def main() -> None:
    # Spawn a local Python subprocess
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    proto = await protocol.Protocol.from_subprocess(process)

    async with proto:
        # Set log level
        await proto(Logger.set_log_level, "INFO")

        # Log a message
        await proto(Logger.log, "INFO", "Listing files in /tmp")

        # Read files
        files = await proto(FileSystem.glob, "/tmp", "*.txt")
        print(f"Found {len(files)} .txt files in /tmp:")
        for f in files[:10]:  # Limit to first 10
            print(f"  {f}")

        # Read a file if it exists
        if files:
            content = await proto(FileSystem.read_str, files[0])
            print(f"\nFirst file content (truncated):\n{content[:200]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] - %(message)s")
    asyncio.run(main())
