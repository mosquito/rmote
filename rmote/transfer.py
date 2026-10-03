"""Incremental file transfer over either rmote client API."""

import asyncio
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from rmote.tools.file_sync import FileSync, SyncResult


async def async_sync_file(
    remote: Callable[..., Any],
    source: str | Path,
    destination: str | Path,
    *,
    direction: Literal["upload", "download"] = "upload",
    block_size: int = 1024 * 1024,
) -> SyncResult:
    """Synchronize one regular file using SHA-256 comparisons at fixed offsets.

    ``remote`` is a Protocol or synchronous Connection. Source is local for
    upload and remote for download. Only differing blocks cross the connection.
    Memory is bounded by block_size; one block is negotiated at a time.

    Destination parents must exist. Symlinks and special files are rejected.
    Replacement is atomic; existing mode/ownership are retained, new files use
    mode 0600. An identical file is left untouched. Metadata is not copied from
    the source. Do not modify either file while syncing: version checks detect
    ordinary concurrent changes, but do not provide filesystem locking.
    """
    if direction not in ("upload", "download"):
        raise ValueError("direction must be 'upload' or 'download'")
    if not 0 < block_size <= 16 * 1024 * 1024:
        raise ValueError("block_size must be between 1 and 16777216")

    cancelled = False

    async def local_call(method: Callable[..., Any], *args: Any) -> Any:
        if cancelled and method != FileSync.close:
            raise asyncio.CancelledError
        return await asyncio.to_thread(method, *args)

    async def remote_call(method: Callable[..., Any], *args: Any) -> Any:
        if cancelled and method != FileSync.close:
            raise asyncio.CancelledError
        value = await asyncio.to_thread(remote, method, *args)
        return await value if inspect.isawaitable(value) else value

    sender, receiver = (local_call, remote_call) if direction == "upload" else (remote_call, local_call)
    source_id, destination_id = uuid4().hex, uuid4().hex

    async def transfer() -> SyncResult:
        try:
            size = await sender(FileSync.begin, source_id, str(source), False, block_size)
            await receiver(FileSync.begin, destination_id, str(destination), True, block_size, size)
            offset = 0
            while offset < size:
                length, digest = await sender(FileSync.signature, source_id)
                if not await receiver(FileSync.match, destination_id, length, digest):
                    data = await sender(FileSync.data, source_id)
                    await receiver(FileSync.write, destination_id, data)
                offset += length
            digest = await sender(FileSync.digest, source_id)
            return cast(SyncResult, await receiver(FileSync.finish, destination_id, digest))
        finally:
            for call, token in ((receiver, destination_id), (sender, source_id)):
                try:
                    await call(FileSync.close, token)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Could not close file transfer session %s", token, exc_info=True
                    )

    # Cancellation waits for the current RPC before closing sessions; cancelling
    # an RPC alone does not stop its remote worker.
    worker = asyncio.create_task(transfer())
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Stop between RPCs, rather than abandoning a thread or a remote write.
        cancelled = True
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def sync_file(
    remote: Callable[..., Any],
    source: str | Path,
    destination: str | Path,
    *,
    direction: Literal["upload", "download"] = "upload",
    block_size: int = 1024 * 1024,
) -> SyncResult:
    """Synchronous counterpart using rmote.sync.Connection, outside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Use async_sync_file inside an event loop")
    return asyncio.run(async_sync_file(remote, source, destination, direction=direction, block_size=block_size))
