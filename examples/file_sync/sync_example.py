"""Run with: uv run python examples/file_sync/sync_example.py.

Use a synchronous Connection from an ordinary script. FileSync has an async API:
asyncio.run drives the transfer, and to_thread adapts blocking Connection calls.
All files and the child process are cleaned up when their context managers exit.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rmote.sync import Connection
from rmote.tools import FileSync


def main() -> None:
    with TemporaryDirectory() as directory, Connection.from_local() as connection:
        root = Path(directory)
        source, target, copy = (root / name for name in ("source", "target", "copy"))
        source.write_bytes(b"AAAABBBBCCCC")

        # FileSync awaits RPC replies. Run each blocking Connection call in a
        # worker thread; keep using Connection's public API, not its internals.
        async def call(method: Callable[..., Any], *args: Any) -> Any:
            return await asyncio.to_thread(connection, method, *args)

        result = asyncio.run(FileSync.upload(call, source, target, block_size=4))
        print("Upload:", result)
        assert result.transferred == 12

        result = asyncio.run(FileSync.upload(call, source, target, block_size=4))
        print("Repeat:", result)
        assert not result.changed and result.transferred == 0

        source.write_bytes(b"AAAAXXXXCCCC")
        result = asyncio.run(FileSync.upload(call, source, target, block_size=4))
        print("One changed block:", result)
        assert result.transferred == 4 and result.reused == 8

        result = asyncio.run(FileSync.download(call, target, copy, block_size=4))
        print("Download:", result)
        assert copy.read_bytes() == source.read_bytes()


if __name__ == "__main__":
    main()
