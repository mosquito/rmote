"""Incremental upload and download with atomic destination replacement."""

import asyncio
import atexit
import hashlib
import logging
import os
import stat
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, ClassVar
from uuid import uuid4

from rmote.protocol import Tool


@dataclass(frozen=True)
class SyncResult:
    """Summary of a completed file synchronization.

    Attributes:
        changed: Whether the destination was created or replaced. False leaves
            its inode and timestamps unchanged; source metadata is not copied.
        size: Final file size in bytes.
        transferred: File-content bytes sent across the connection, before
            compression. Excludes signatures, RPC framing and transferred code.
        reused: Bytes copied from the existing destination into the temporary
            file. ``transferred + reused == size`` even when only truncating.
    """

    changed: bool
    size: int
    transferred: int
    reused: int


def _version(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _open_regular(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class Session:
    """One sender or receiver in the block exchange used by :class:`FileSync`.

    Opens a regular file and keeps at most one block of content in memory.
    A receiver also opens a temporary file beside the destination. Always call
    :meth:`close` in a ``finally`` block, including after successful completion.
    Calls on one session must be sequential.

    Args:
        path: Source or destination path in this process's filesystem.
        receiving: True to assemble a destination, False to read a source.
        block_size: Block size in bytes, from 1 through 16 MiB inclusive.
        size: Expected final size for a receiver; ignored by a sender, whose
            size is read from the open source file.

    Raises:
        ValueError: Invalid block size, negative size, or a special file.
        OSError: Opening the file fails, including missing source or parent,
            insufficient permissions, or a symlink at the supplied path.

    :meth:`step` advances the exchange and returns the message for the other
    session. A receiver returns :class:`SyncResult` after verifying the complete
    file and committing it. Application code normally uses :class:`FileSync`.
    """

    def __init__(self, path: str, receiving: bool, block_size: int, size: int = 0) -> None:
        if not 0 < block_size <= 16 * 1024 * 1024:
            raise ValueError("block_size must be between 1 and 16777216")
        if size < 0:
            raise ValueError("size must be non-negative")
        self.path = Path(path).absolute()
        self.receiving = receiving
        self.block_size = block_size
        self.old: BinaryIO | None = None
        self.output: BinaryIO | None = None
        self.temp: Path | None = None
        self.initial: os.stat_result | None = None
        self.offset = 0
        self.pending: bytes | None = None
        self.expected: tuple[int, bytes] | None = None
        self.digest = hashlib.sha256()
        self.transferred = 0
        self.reused = 0
        try:
            try:
                self.old = _open_regular(self.path)
                self.initial = os.fstat(self.old.fileno())
            except FileNotFoundError:
                if not receiving:
                    raise
            self.size = size if receiving else self.initial.st_size if self.initial else 0
            if receiving:
                fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.rmote-", dir=self.path.parent)
                self.temp = Path(name)
                self.output = os.fdopen(fd, "w+b")
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        """Raise RuntimeError if the file's identity, size or timestamps changed.

        These checks detect ordinary concurrent writes; they do not lock the
        file or make a snapshot. Keep both files stable throughout the transfer.
        """
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            current = None
        if self.initial is None:
            if current is not None:
                raise RuntimeError("Destination appeared during transfer")
        elif current is None or _version(current) != _version(self.initial):
            raise RuntimeError("File changed during transfer")
        if self.old is not None and self.initial is not None:
            if _version(os.fstat(self.old.fileno())) != _version(self.initial):
                raise RuntimeError("Open file changed during transfer")

    def step(self, message: Any = None) -> Any:
        """Exchange a signature, match reply, or requested block.

        Start the sender with None. It returns ``(length, sha256_digest)``.
        Feed this to the receiver: True means its old block matched and was
        copied; False requests the pending source bytes. Pass that reply to the
        sender. On False it returns those bytes; after writing and verifying
        them the receiver replies None. True or None advances the sender.

        A zero-length signature ends the stream and carries the whole-file hash.
        The receiver then returns SyncResult. Stop exchanging messages at that
        point and close both sessions.

        Raises:
            ValueError: Invalid signature, unexpected/corrupt content, incomplete
                transfer, or mismatched whole-file digest.
            RuntimeError: Either this file or its open descriptor changed.
            OSError: A read, write, fsync, metadata update or replacement fails.
        """
        self.check()
        if not self.receiving:
            if message is False:
                if self.pending is None:
                    raise ValueError("No pending source block")
                return self.pending
            if self.offset == self.size:
                return 0, self.digest.digest()
            assert self.old is not None
            length = min(self.block_size, self.size - self.offset)
            data = self.old.read(length)
            if len(data) != length:
                raise RuntimeError("Source size changed during transfer")
            self.pending = data
            self.offset += length
            self.digest.update(data)
            return length, hashlib.sha256(data).digest()

        if isinstance(message, bytes):
            if self.expected != (len(message), hashlib.sha256(message).digest()):
                raise ValueError("Block does not match its signature")
            self.append(message)
            self.transferred += len(message)
            self.expected = None
            return None
        length, digest = message
        if length == 0:
            return self.finish(digest)
        if self.expected is not None or length != min(self.block_size, self.size - self.offset) or length <= 0:
            raise ValueError("Invalid block signature")
        data = self.old.read(length) if self.old is not None else b""
        if len(data) == length and hashlib.sha256(data).digest() == digest:
            self.append(data)
            self.reused += length
            return True
        self.expected = length, digest
        return False

    def append(self, data: bytes) -> None:
        """Append already verified bytes to a receiver's temporary file.

        Updates its position and cumulative SHA-256. Use step for normal
        exchange: this helper does not validate a block signature or count it
        as transferred/reused.
        """
        assert self.output is not None
        self.output.write(data)
        self.digest.update(data)
        self.offset += len(data)

    def finish(self, digest: bytes) -> SyncResult:
        """Verify the receiver's final SHA-256 and atomically install its output.

        All bytes must have arrived and no requested block may be outstanding.
        An identical destination is left untouched. Otherwise flush and fsync
        the temporary file, preserve existing mode/uid/gid, and use os.replace.
        New files have mode 0600. The containing directory is not fsynced, so
        this is atomic replacement, not a power-loss durability guarantee.

        Raises:
            ValueError: Incomplete transfer, wrong digest or a sender session.
            RuntimeError: The destination changed during transfer.
            OSError: Flushing, metadata preservation or replacement fails.

        Call close afterwards to release descriptors and any temporary file.
        """
        if not self.receiving or self.expected is not None or self.offset != self.size:
            raise ValueError("Incomplete transfer")
        if self.digest.digest() != digest:
            raise ValueError("File digest mismatch")
        self.check()
        changed = self.initial is None or self.initial.st_size != self.size or self.transferred > 0
        if changed:
            assert self.output is not None and self.temp is not None
            self.output.flush()
            # Preserve destination ownership and permissions. New files are private.
            if self.initial is not None:
                st = os.fstat(self.output.fileno())
                if (st.st_uid, st.st_gid) != (self.initial.st_uid, self.initial.st_gid):
                    os.fchown(self.output.fileno(), self.initial.st_uid, self.initial.st_gid)
                os.fchmod(self.output.fileno(), stat.S_IMODE(self.initial.st_mode))
            os.fsync(self.output.fileno())
            self.check()
            os.replace(self.temp, self.path)
        return SyncResult(changed, self.size, self.transferred, self.reused)

    def close(self) -> None:
        """Close file descriptors and remove any remaining temporary file.

        Does not install incomplete output or undo a completed replacement.
        Repeated calls are safe. Filesystem errors during cleanup propagate.
        """
        try:
            if self.old is not None:
                self.old.close()
            if self.output is not None:
                self.output.close()
        finally:
            if self.temp is not None:
                self.temp.unlink(missing_ok=True)


class FileSync(Tool):
    """Synchronize regular files through an open asynchronous Protocol.

    Call ``await FileSync.upload(protocol, local_path, remote_path)`` or
    ``await FileSync.download(protocol, remote_path, local_path)`` directly.
    These methods coordinate both machines locally; do not pass them as the
    tool argument to ``protocol(...)``. The target needs Python but no rmote
    installation. Paths are interpreted on their respective sides; parent
    directories must already exist and symlinks/special files are rejected.

    The sender hashes a block with SHA-256; the receiver compares its own block
    at that offset and requests content only on mismatch. Blocks default to
    1 MiB and are negotiated one at a time. Memory is bounded by block size,
    but network latency limits throughput. Insertions can shift later block
    boundaries and cause large retransfers.

    The receiver assembles a temporary file beside the destination, verifies
    the cumulative digest, flushes and fsyncs, then atomically replaces it.
    Existing destination mode/uid/gid are preserved, new files use 0600. Source
    timestamps, ACLs and extended attributes are not copied. Other hard links
    still point to the old file. Identical files retain inode and timestamps,
    though comparison reads both files and writes a temporary copy. Allow free
    space for the complete result, even if only one block differs.

    Keep both files stable: version checks detect ordinary concurrent changes
    but do not provide locking. Errors before replacement preserve the target.
    Cancellation waits for the current RPC before cleanup and can therefore
    wait on a stalled connection. An in-flight final replacement cannot be
    undone. Lost connections or abrupt process termination may leave a
    ``.<filename>.rmote-*`` temporary file; retry after an ambiguous final RPC
    failure to establish the resulting state. There is no resume or directory
    synchronization, and no guarantee of durability across power loss.

    Example using a real local subprocess (no SSH server required):

        >>> import asyncio
        >>> import sys
        >>> from pathlib import Path
        >>> from tempfile import TemporaryDirectory
        >>> from rmote.protocol import Protocol
        >>> async def example():
        ...     with TemporaryDirectory() as directory:
        ...         root = Path(directory)
        ...         source, target, copy = (root / n for n in ("source", "target", "copy"))
        ...         source.write_bytes(b"hello")
        ...         process = await asyncio.create_subprocess_exec(
        ...             sys.executable, "-qui", stdin=asyncio.subprocess.PIPE,
        ...             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        ...         )
        ...         try:
        ...             protocol = await Protocol.from_subprocess(process)
        ...             async with protocol:
        ...                 first = await FileSync.upload(protocol, source, target)
        ...                 again = await FileSync.upload(protocol, source, target)
        ...                 await FileSync.download(protocol, target, copy)
        ...                 return first.changed, again.changed, copy.read_bytes()
        ...         finally:
        ...             if process.returncode is None:
        ...                 process.terminate()
        ...             await process.wait()
        >>> asyncio.run(example())
        (True, False, b'hello')

    With SSH, enter ``async with await Protocol.from_ssh("user@host") as protocol``
    and use the same upload/download calls. The SSH protocol owns its process;
    the local subprocess example explicitly owns and reaps its child.
    """

    _sessions: ClassVar[dict[str, Session]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()

    @staticmethod
    async def upload(
        protocol: Callable[..., Awaitable[Any]],
        local_path: str | Path,
        remote_path: str | Path,
        *,
        block_size: int = 1024 * 1024,
    ) -> SyncResult:
        """Synchronize local source content into a remote destination.

        Args:
            protocol: An open async Protocol, already entered with async with.
            local_path: Existing local source file; relative to local cwd.
            remote_path: Remote destination file; relative to the target cwd.
                Its parent must exist. A missing file is created with mode 0600.
            block_size: Bytes per block, 1 through 16 MiB; defaults to 1 MiB.

        Returns:
            SyncResult with changed status and transferred/reused byte counts.
            Repeating the call with identical content returns changed=False.

        Raises:
            ValueError: Invalid block size, special file, or failed integrity check.
            RuntimeError: A file changed while being synchronized.
            OSError: File access, temporary-file creation or installation fails.
            ConnectionError: The connection is closed. Transport failures may
                also propagate their original exception.
            asyncio.CancelledError: Cancelled after the in-flight RPC and cleanup.

        Uses the atomic replacement and metadata policy documented on FileSync.
        """
        return await FileSync.transfer(protocol, local_path, remote_path, True, block_size)

    @staticmethod
    async def download(
        protocol: Callable[..., Awaitable[Any]],
        remote_path: str | Path,
        local_path: str | Path,
        *,
        block_size: int = 1024 * 1024,
    ) -> SyncResult:
        """Synchronize remote source content into a local destination.

        Args:
            protocol: An open async Protocol, already entered with async with.
            remote_path: Existing remote source file; relative to target cwd.
            local_path: Local destination file; relative to local cwd. Its parent
                must exist. A missing file is created with mode 0600.
            block_size: Bytes per block, 1 through 16 MiB; defaults to 1 MiB.

        Returns:
            SyncResult with changed status and transferred/reused byte counts.

        Raises:
            ValueError: Invalid block size, special file, or failed integrity check.
            RuntimeError: A file changed while being synchronized.
            OSError: File access, temporary-file creation or installation fails.
            ConnectionError: Closed connection; other transport errors propagate.
            asyncio.CancelledError: Cancelled after the in-flight RPC and cleanup.

        Uses the same exchange and atomic replacement as upload, with the remote
        side as sender. Call directly: ``await FileSync.download(protocol, src, dst)``.
        """
        return await FileSync.transfer(protocol, local_path, remote_path, False, block_size)

    @staticmethod
    async def transfer(
        protocol: Callable[..., Awaitable[Any]],
        local_path: str | Path,
        remote_path: str | Path,
        uploading: bool,
        block_size: int,
    ) -> SyncResult:
        token = uuid4().hex
        cancelled = False

        async def run() -> SyncResult:
            local = None
            try:
                if uploading:
                    local = await asyncio.to_thread(Session, str(local_path), False, block_size)
                    await protocol(FileSync._open, token, str(remote_path), True, block_size, local.size)
                else:
                    size = await protocol(FileSync._open, token, str(remote_path), False, block_size)
                    local = await asyncio.to_thread(Session, str(local_path), True, block_size, size)
                message = None
                # Both sides run the same block exchange. Upload starts locally;
                # download starts remotely. Only one block is held on each side.
                while True:
                    for remote in (not uploading, uploading):
                        if cancelled:
                            raise asyncio.CancelledError
                        message = (
                            await protocol(FileSync._step, token, message)
                            if remote
                            else await asyncio.to_thread(local.step, message)
                        )
                        if isinstance(message, SyncResult):
                            return message
            finally:
                try:
                    if local is not None:
                        await asyncio.to_thread(local.close)
                finally:
                    try:
                        await protocol(FileSync._close, token)
                    except Exception:
                        logging.getLogger(__name__).warning("Could not close remote file transfer", exc_info=True)

        # RPC cancellation does not stop a remote worker. Finish the current
        # exchange before cleaning up, without starting the next operation.
        worker = asyncio.create_task(run())
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
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

    @classmethod
    def _open(cls, token: str, path: str, receiving: bool, block_size: int, size: int = 0) -> int:
        with cls._lock:
            if token in cls._sessions:
                raise ValueError("Session already exists")
            session = Session(path, receiving, block_size, size)
            cls._sessions[token] = session
            return session.size

    @classmethod
    def _step(cls, token: str, message: Any = None) -> Any:
        with cls._lock:
            return cls._sessions[token].step(message)

    @classmethod
    def _close(cls, token: str) -> None:
        with cls._lock:
            session = cls._sessions.pop(token, None)
            if session is not None:
                session.close()


def _cleanup() -> None:
    for token in list(FileSync._sessions):
        FileSync._close(token)


atexit.register(_cleanup)
