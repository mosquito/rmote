"""Run a local async client with explicit subprocess ownership."""

import asyncio
import logging
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from rmote.protocol import Protocol
from rmote.tools import FileSystem, Logger


async def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "sample.txt"
        path.write_text("hello\n", encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-qui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with await Protocol.from_subprocess(process) as remote:
                await remote(Logger.set_log_level, "INFO")
                await remote(Logger.log, "INFO", "Reading a sample file")
                files = await remote(FileSystem.glob, directory, "*.txt")
                assert files == [str(path)]
                content = await remote(FileSystem.read_str, str(path))
                assert content == "hello\n"
                print(content, end="")
        finally:
            # The caller owns the subprocess passed to from_subprocess.
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    asyncio.run(main())
