"""Run with python examples/local_async.py after installing rmote."""

import asyncio
import sys

from lifecycle_tools import Echo

from rmote.protocol import Protocol


async def main() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-qui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with await Protocol.from_subprocess(process) as remote:
            assert await remote(Echo.echo, "hello") == "hello"
            assert await remote(Echo.later, "again") == "again"
            async with asyncio.timeout(2.0):
                assert await remote(Echo.echo, "deadline") == "deadline"
    finally:
        # from_subprocess closes the channel; the caller owns this process.
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
