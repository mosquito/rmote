"""Run with: uv run python examples/file_sync/async_example.py.

Upload, repeat, change one block, and download through a local Python subprocess.
All files live in a temporary directory; no SSH server or root access is needed.
"""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from rmote.protocol import Protocol
from rmote.tools import FileSync


async def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source, target, copy = (root / name for name in ("source", "target", "copy"))
        source.write_bytes(b"AAAABBBBCCCC")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-qui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with await Protocol.from_subprocess(process) as protocol:
                # Four-byte blocks make the incremental transfer easy to see.
                result = await FileSync.upload(protocol, source, target, block_size=4)
                print("Upload:", result)
                assert result.transferred == 12

                result = await FileSync.upload(protocol, source, target, block_size=4)
                print("Repeat:", result)
                assert not result.changed and result.transferred == 0

                source.write_bytes(b"AAAAXXXXCCCC")
                result = await FileSync.upload(protocol, source, target, block_size=4)
                print("One changed block:", result)
                assert result.transferred == 4 and result.reused == 8

                result = await FileSync.download(protocol, target, copy, block_size=4)
                print("Download:", result)
                assert copy.read_bytes() == source.read_bytes()
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
