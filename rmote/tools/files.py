"""Session-owned remote file descriptors for interactive file access."""

import atexit
import contextlib
import errno
import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import IntFlag
from typing import ClassVar
from uuid import uuid4

from rmote.protocol import Tool


class OpenFlags(IntFlag):
    """SFTP v3 file open flags, independent of the remote OS."""

    READ = 0x01
    WRITE = 0x02
    APPEND = 0x04
    CREAT = 0x08
    TRUNC = 0x10
    EXCL = 0x20
    ALL = READ | WRITE | APPEND | CREAT | TRUNC | EXCL


@dataclass
class Handle:
    fd: int
    append: bool = False
    entries: Iterator[os.DirEntry[str]] | None = None
    resources: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)

    def close(self) -> None:
        try:
            self.resources.close()
        finally:
            os.close(self.fd)


@dataclass
class FileSession:
    handles: dict[bytes, Handle] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False

    def get(self, handle: bytes, directory: bool = False) -> Handle:
        result = self.handles.get(handle)
        if result is None or (result.entries is not None) != directory:
            raise OSError(errno.EBADF, "Invalid file handle")
        return result

    def add(self, handle: Handle) -> bytes:
        key = uuid4().bytes
        self.handles[key] = handle
        return key


def set_attributes(target: str | int, attrs: dict[str, int]) -> None:
    """Apply only the supplied attributes; permission changes follow chown."""
    if "size" in attrs:
        os.truncate(target, attrs["size"])
    if "uid" in attrs:
        if isinstance(target, int):
            os.fchown(target, attrs["uid"], attrs["gid"])
        else:
            os.chown(target, attrs["uid"], attrs["gid"])
    if "permissions" in attrs:
        if isinstance(target, int):
            os.fchmod(target, attrs["permissions"] & 0o7777)
        else:
            os.chmod(target, attrs["permissions"] & 0o7777)
    if "atime" in attrs:
        os.utime(target, (attrs["atime"], attrs["mtime"]))


class Files(Tool):
    """Keep file and directory handles within an explicit session.

    Start a session and pass its session_id on every operation. Always call
    release(), including after cancellation. Handles
    are opaque and cannot be used by another session. Paths use the remote
    process's working directory and permissions; this tool is not a sandbox.

    Synchronous methods run in the protocol executor. A per-session lock
    serializes access with release; independent sessions can run concurrently.
    The owning remote process also releases descriptors when it exits.
    """

    MAX_HANDLES: ClassVar[int] = 128
    MAX_IO: ClassVar[int] = 256 * 1024
    _sessions: ClassVar[dict[str, FileSession]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()

    @classmethod
    def start(cls, session_id: str | None = None) -> str:
        """Return the ID of a new empty session.

        A caller may supply its own unique ID when it needs to release a
        session after a lost or cancelled start response. Existing IDs fail.
        """
        session_id = uuid4().hex if session_id is None else session_id
        with cls._lock:
            if session_id in cls._sessions:
                raise ValueError("File session already exists")
            cls._sessions[session_id] = FileSession()
        return session_id

    @classmethod
    @contextlib.contextmanager
    def session(cls, session_id: str) -> Iterator[FileSession]:
        with cls._lock:
            session = cls._sessions.get(session_id)
        if session is None:
            raise OSError(errno.EBADF, "Unknown file session")
        with session.lock:
            if session.closed:
                raise OSError(errno.EBADF, "File session is closed")
            yield session

    @classmethod
    def release(cls, session_id: str) -> None:
        """Close all handles, including directory iterators. Safe to repeat."""
        with cls._lock:
            session = cls._sessions.pop(session_id, None)
        if session is None:
            return
        with session.lock:
            session.closed = True
            handles, session.handles = session.handles, {}
            error = None
            for handle in handles.values():
                try:
                    handle.close()
                except OSError as exc:
                    error = exc
            if error is not None:
                raise error

    @classmethod
    def open(cls, session_id: str, path: str, flags: int, mode: int = 0o666) -> bytes:
        """Open a regular file with SFTP v3 flags and return an opaque handle.

        Combine OpenFlags members; integer wire values are also accepted.
        The flags are translated on the remote OS, never on the caller's OS.
        Special files are rejected; nonblocking open prevents a FIFO hang.
        """
        # Invert the integer mask: IntFlag inversion omits unknown bits.
        if int(flags) & ~int(OpenFlags.ALL):
            raise ValueError("Invalid file open flags")
        flags = OpenFlags(flags)
        read_write = OpenFlags.READ | OpenFlags.WRITE
        if not flags & read_write or (flags & (OpenFlags.TRUNC | OpenFlags.EXCL) and not flags & OpenFlags.CREAT):
            raise ValueError("Invalid file open flags")
        if flags & (OpenFlags.APPEND | OpenFlags.TRUNC) and not flags & OpenFlags.WRITE:
            raise ValueError("Write access is required for append or truncate")
        access = (
            os.O_RDWR if flags & read_write == read_write else os.O_WRONLY if flags & OpenFlags.WRITE else os.O_RDONLY
        )
        for bit, native in (
            (OpenFlags.APPEND, os.O_APPEND),
            (OpenFlags.CREAT, os.O_CREAT),
            (OpenFlags.TRUNC, os.O_TRUNC),
            (OpenFlags.EXCL, os.O_EXCL),
        ):
            if flags & bit:
                access |= native
        with cls.session(session_id) as session:
            if len(session.handles) >= cls.MAX_HANDLES:
                raise OSError(errno.EMFILE, "File session handle limit reached")
            fd = os.open(path or ".", access | os.O_NONBLOCK | os.O_NOCTTY, mode & 0o7777)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EOPNOTSUPP, "Only regular files can be opened")
                return session.add(Handle(fd, append=bool(flags & OpenFlags.APPEND)))
            except BaseException:
                os.close(fd)
                raise

    @classmethod
    def close(cls, session_id: str, handle: bytes) -> None:
        with cls.session(session_id) as session:
            item = session.handles.pop(handle, None)
            if item is None:
                raise OSError(errno.EBADF, "Invalid file handle")
            item.close()

    @classmethod
    def read(cls, session_id: str, handle: bytes, offset: int, size: int) -> bytes:
        if offset < 0 or not 0 <= size <= cls.MAX_IO:
            raise ValueError("Invalid read range")
        with cls.session(session_id) as session:
            return os.pread(session.get(handle).fd, size, offset)

    @classmethod
    def write(cls, session_id: str, handle: bytes, offset: int, data: bytes) -> None:
        """Complete every byte of a write before returning success."""
        if offset < 0 or len(data) > cls.MAX_IO:
            raise ValueError("Invalid write range")
        with cls.session(session_id) as session:
            item = session.get(handle)
            view = memoryview(data)
            while view:
                count = os.write(item.fd, view) if item.append else os.pwrite(item.fd, view, offset)
                if count <= 0:
                    raise OSError(errno.EIO, "File write made no progress")
                view = view[count:]
                offset += count

    @classmethod
    def stat(cls, session_id: str, path: str, follow: bool = True) -> os.stat_result:
        with cls.session(session_id):
            return os.stat(path or ".", follow_symlinks=follow)

    @classmethod
    def fstat(cls, session_id: str, handle: bytes) -> os.stat_result:
        with cls.session(session_id) as session:
            return os.fstat(session.get(handle).fd)

    @classmethod
    def setstat(cls, session_id: str, path: str, attrs: dict[str, int]) -> None:
        with cls.session(session_id):
            set_attributes(path or ".", attrs)

    @classmethod
    def fsetstat(cls, session_id: str, handle: bytes, attrs: dict[str, int]) -> None:
        with cls.session(session_id) as session:
            set_attributes(session.get(handle).fd, attrs)

    @classmethod
    def opendir(cls, session_id: str, path: str) -> bytes:
        with cls.session(session_id) as session:
            if len(session.handles) >= cls.MAX_HANDLES:
                raise OSError(errno.EMFILE, "File session handle limit reached")
            fd = os.open(path or ".", os.O_RDONLY | os.O_DIRECTORY)
            handle = Handle(fd)
            try:
                handle.entries = handle.resources.enter_context(os.scandir(fd))
                return session.add(handle)
            except BaseException:
                handle.close()
                raise

    @classmethod
    def readdir(cls, session_id: str, handle: bytes) -> list[tuple[str, os.stat_result]]:
        """Return at most 64 entries. An empty batch marks end of directory."""
        with cls.session(session_id) as session:
            entries = session.get(handle, directory=True).entries
            assert entries is not None
            batch: list[tuple[str, os.stat_result]] = []
            while len(batch) < 64:
                entry = next(entries, None)
                if entry is None:
                    break
                try:
                    batch.append((entry.name, entry.stat(follow_symlinks=False)))
                except FileNotFoundError:
                    continue
            return batch

    @classmethod
    def realpath(cls, session_id: str, path: str) -> str:
        with cls.session(session_id):
            return os.path.realpath(path or ".")

    @classmethod
    def readlink(cls, session_id: str, path: str) -> str:
        with cls.session(session_id):
            return os.readlink(path)

    @classmethod
    def symlink(cls, session_id: str, target: str, path: str) -> None:
        with cls.session(session_id):
            os.symlink(target, path)

    @classmethod
    def mkdir(cls, session_id: str, path: str, mode: int = 0o777) -> None:
        with cls.session(session_id):
            os.mkdir(path, mode & 0o7777)

    @classmethod
    def remove(cls, session_id: str, path: str, directory: bool = False) -> None:
        with cls.session(session_id):
            if directory:
                os.rmdir(path)
            else:
                os.unlink(path)

    @classmethod
    def rename(cls, session_id: str, source: str, target: str, replace: bool = False) -> None:
        with cls.session(session_id):
            if replace:
                os.replace(source, target)
            elif stat.S_ISREG(os.lstat(source).st_mode):
                # link/unlink supplies no-clobber semantics for regular files.
                os.link(source, target, follow_symlinks=False)
                os.unlink(source)
            else:
                if os.path.lexists(target):
                    raise FileExistsError(errno.EEXIST, "Destination already exists", target)
                os.rename(source, target)

    @classmethod
    def fsync(cls, session_id: str, handle: bytes) -> None:
        with cls.session(session_id) as session:
            os.fsync(session.get(handle).fd)


@atexit.register
def close_sessions() -> None:
    for session_id in list(Files._sessions):
        with contextlib.suppress(OSError):
            Files.release(session_id)
