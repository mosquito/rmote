"""SFTP v3 requests over mux stdio, backed by remote Files sessions."""

import asyncio
import contextlib
import errno
import os
import stat
import struct
import time
from collections.abc import Awaitable, Callable
from enum import IntEnum, IntFlag
from typing import Any
from uuid import uuid4

from rmote.tools.files import Files

UINT32 = struct.Struct(">I")
UINT64 = struct.Struct(">Q")
MAX_PACKET = Files.MAX_IO + 1024


class Message(IntEnum):
    INIT = 1
    VERSION = 2
    OPEN = 3
    CLOSE = 4
    READ = 5
    WRITE = 6
    LSTAT = 7
    FSTAT = 8
    SETSTAT = 9
    FSETSTAT = 10
    OPENDIR = 11
    READDIR = 12
    REMOVE = 13
    MKDIR = 14
    RMDIR = 15
    REALPATH = 16
    STAT = 17
    RENAME = 18
    READLINK = 19
    SYMLINK = 20
    STATUS = 101
    HANDLE = 102
    DATA = 103
    NAME = 104
    ATTRS = 105
    EXTENDED = 200


class Status(IntEnum):
    OK = 0
    EOF = 1
    NO_SUCH_FILE = 2
    PERMISSION_DENIED = 3
    FAILURE = 4
    BAD_MESSAGE = 5
    OP_UNSUPPORTED = 8


class AttrFlags(IntFlag):
    """SFTP v3 attribute fields present in a packet."""

    SIZE = 0x00000001
    UIDGID = 0x00000002
    PERMISSIONS = 0x00000004
    ACMODTIME = 0x00000008
    EXTENDED = 0x80000000
    ALL = SIZE | UIDGID | PERMISSIONS | ACMODTIME | EXTENDED


class Packet:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.offset:
            raise ValueError("Truncated SFTP packet")
        start = self.offset
        self.offset += size
        return self.data[start : self.offset]

    def uint32(self) -> int:
        return int(UINT32.unpack(self.take(4))[0])

    def uint64(self) -> int:
        return int(UINT64.unpack(self.take(8))[0])

    def string(self) -> bytes:
        return self.take(self.uint32())

    def path(self) -> str:
        data = self.string()
        if b"\0" in data:
            raise ValueError("NUL in path")
        return data.decode("utf-8", "surrogateescape")

    def attrs(self) -> dict[str, int]:
        flags = self.uint32()
        if flags & ~int(AttrFlags.ALL):
            raise ValueError("Unknown attribute flags")
        flags = AttrFlags(flags)
        result = {}
        if flags & AttrFlags.SIZE:
            result["size"] = self.uint64()
        if flags & AttrFlags.UIDGID:
            result["uid"], result["gid"] = self.uint32(), self.uint32()
        if flags & AttrFlags.PERMISSIONS:
            result["permissions"] = self.uint32()
        if flags & AttrFlags.ACMODTIME:
            result["atime"], result["mtime"] = self.uint32(), self.uint32()
        if flags & AttrFlags.EXTENDED:
            for _ in range(self.uint32()):
                self.string()
                self.string()
            raise NotImplementedError("Extended attributes are not supported")
        return result

    def end(self) -> None:
        if self.offset != len(self.data):
            raise ValueError("Unexpected data in SFTP packet")


def string(value: bytes | str) -> bytes:
    data = value.encode("utf-8", "surrogateescape") if isinstance(value, str) else value
    return UINT32.pack(len(data)) + data


def attributes(value: os.stat_result) -> bytes:
    return struct.pack(
        ">IQIIIII",
        AttrFlags.SIZE | AttrFlags.UIDGID | AttrFlags.PERMISSIONS | AttrFlags.ACMODTIME,
        value.st_size,
        value.st_uid,
        value.st_gid,
        value.st_mode,
        max(0, min(int(value.st_atime), 0xFFFFFFFF)),
        max(0, min(int(value.st_mtime), 0xFFFFFFFF)),
    )


def name_entry(name: str, attrs: os.stat_result | None = None) -> bytes:
    # SFTP v3 includes the display form used by clients for `ls -l`.
    if attrs is None:
        return string(name) + string(name) + UINT32.pack(0)
    date = time.strftime("%b %d %H:%M", time.localtime(attrs.st_mtime))
    longname = (
        f"{stat.filemode(attrs.st_mode)} {attrs.st_nlink} {attrs.st_uid} {attrs.st_gid} {attrs.st_size} {date} {name}"
    )
    return string(name) + string(longname) + attributes(attrs)


class SftpServer:
    """Serve one SFTP session with bounded packets and ordered requests.

    One request is in flight per session. Reading stops while it runs, so the
    stdio pipe provides backpressure to pipelined clients. Separate sessions
    share the rmote connection but have independent file handles.
    """

    def __init__(
        self,
        protocol: Callable[..., Awaitable[Any]],
        read: Callable[[int], Awaitable[bytes]],
        write: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.protocol = protocol
        self.read = read
        self.write = write
        self.session_id = uuid4().hex

    async def call(self, method: Any, *args: Any) -> Any:
        # Cancellation of an RPC does not stop its executor worker. Await its
        # result before release(), so a late OPEN cannot leak a descriptor.
        future = asyncio.ensure_future(self.protocol(method, self.session_id, *args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()
            raise

    async def exact(self, size: int, *, allow_eof: bool = False) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = await self.read(size - len(data))
            if not chunk:
                if allow_eof and not data:
                    raise EOFError("SFTP input closed")
                raise ValueError("Truncated SFTP packet")
            data.extend(chunk)
        return bytes(data)

    async def send(self, kind: Message, request: int, data: bytes = b"") -> None:
        payload = bytes((kind,)) + UINT32.pack(request) + data
        await self.write(UINT32.pack(len(payload)) + payload)

    async def status(self, request: int, code: Status, message: str = "") -> None:
        await self.send(Message.STATUS, request, UINT32.pack(code) + string(message) + string(b""))

    async def run(self) -> int:
        try:
            await self.call(Files.start)
            initialized = False
            while True:
                size = UINT32.unpack(await self.exact(4, allow_eof=True))[0]
                if not 5 <= size <= MAX_PACKET:
                    raise ValueError("Invalid SFTP packet length")
                packet = Packet(await self.exact(size))
                kind = packet.take(1)[0]
                request = packet.uint32()
                if not initialized:
                    if kind != Message.INIT or request < 3:
                        raise ValueError("Expected SFTP version 3 or later")
                    while packet.offset < len(packet.data):
                        packet.string()
                        packet.string()
                    extensions = b"".join(
                        string(name) + string("1") for name in ("posix-rename@openssh.com", "fsync@openssh.com")
                    )
                    await self.send(Message.VERSION, 3, extensions)
                    initialized = True
                    continue
                await self.request(kind, request, packet)
        except EOFError:
            return 0
        finally:
            with contextlib.suppress(ConnectionError):
                await self.call(Files.release)

    async def request(self, kind: int, request: int, packet: Packet) -> None:
        try:
            await self.dispatch(kind, request, packet)
        except (ValueError, OverflowError, struct.error) as exc:
            await self.status(request, Status.BAD_MESSAGE, str(exc))
        except NotImplementedError as exc:
            await self.status(request, Status.OP_UNSUPPORTED, str(exc))
        except OSError as exc:
            code = {
                errno.ENOENT: Status.NO_SUCH_FILE,
                errno.ENOTDIR: Status.NO_SUCH_FILE,
                errno.EACCES: Status.PERMISSION_DENIED,
                errno.EPERM: Status.PERMISSION_DENIED,
                errno.ENOSYS: Status.OP_UNSUPPORTED,
                errno.EOPNOTSUPP: Status.OP_UNSUPPORTED,
            }.get(exc.errno or 0, Status.FAILURE)
            await self.status(request, code, str(exc))

    async def dispatch(self, kind: int, request: int, packet: Packet) -> None:
        match kind:
            case Message.OPEN:
                path, flags, attrs = packet.path(), packet.uint32(), packet.attrs()
                packet.end()
                handle = await self.call(Files.open, path, flags, attrs.get("permissions", 0o666))
                await self.send(Message.HANDLE, request, string(handle))
                return
            case Message.CLOSE:
                handle = packet.string()
                packet.end()
                await self.call(Files.close, handle)
            case Message.READ:
                handle, offset, size = packet.string(), packet.uint64(), packet.uint32()
                packet.end()
                data = await self.call(Files.read, handle, offset, min(size, Files.MAX_IO))
                if data or size == 0:
                    await self.send(Message.DATA, request, string(data))
                else:
                    await self.status(request, Status.EOF)
                return
            case Message.WRITE:
                handle, offset, data = packet.string(), packet.uint64(), packet.string()
                packet.end()
                await self.call(Files.write, handle, offset, data)
            case Message.STAT | Message.LSTAT | Message.FSTAT:
                target = packet.string() if kind == Message.FSTAT else packet.path()
                packet.end()
                attrs = (
                    await self.call(Files.fstat, target)
                    if kind == Message.FSTAT
                    else await self.call(Files.stat, target, kind == Message.STAT)
                )
                await self.send(Message.ATTRS, request, attributes(attrs))
                return
            case Message.SETSTAT | Message.FSETSTAT:
                target = packet.string() if kind == Message.FSETSTAT else packet.path()
                values = packet.attrs()
                packet.end()
                await self.call(Files.fsetstat if kind == Message.FSETSTAT else Files.setstat, target, values)
            case Message.OPENDIR:
                path = packet.path()
                packet.end()
                handle = await self.call(Files.opendir, path)
                await self.send(Message.HANDLE, request, string(handle))
                return
            case Message.READDIR:
                handle = packet.string()
                packet.end()
                entries = await self.call(Files.readdir, handle)
                if entries:
                    data = UINT32.pack(len(entries)) + b"".join(name_entry(name, st) for name, st in entries)
                    await self.send(Message.NAME, request, data)
                else:
                    await self.status(request, Status.EOF)
                return
            case Message.REMOVE | Message.RMDIR:
                path = packet.path()
                packet.end()
                await self.call(Files.remove, path, kind == Message.RMDIR)
            case Message.MKDIR:
                path, values = packet.path(), packet.attrs()
                packet.end()
                await self.call(Files.mkdir, path, values.get("permissions", 0o777))
            case Message.REALPATH | Message.READLINK:
                path = packet.path()
                packet.end()
                result = await self.call(Files.realpath if kind == Message.REALPATH else Files.readlink, path)
                await self.send(Message.NAME, request, UINT32.pack(1) + name_entry(result))
                return
            case Message.RENAME | Message.SYMLINK:
                source, target = packet.path(), packet.path()
                packet.end()
                # OpenSSH uses target, linkpath for SYMLINK, opposite to the v3 draft.
                await self.call(Files.rename if kind == Message.RENAME else Files.symlink, source, target)
            case Message.EXTENDED:
                match packet.string():
                    case b"posix-rename@openssh.com":
                        source, target = packet.path(), packet.path()
                        packet.end()
                        await self.call(Files.rename, source, target, True)
                    case b"fsync@openssh.com":
                        handle = packet.string()
                        packet.end()
                        await self.call(Files.fsync, handle)
                    case _:
                        raise NotImplementedError("Unsupported SFTP extension")
            case _:
                raise NotImplementedError("Unsupported SFTP request")
        await self.status(request, Status.OK)
