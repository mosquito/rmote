"""Compression must pay for itself before a packet carries it.

A packet never grows on the wire, random data is not compressed at all, and a
small payload does not pay for a hand-off to a worker thread.

This is the packet policy, which compresses each payload on its own. The
frame codec of `FRAME_CODEC` replaces it by default, so every protocol here
turns that codec off. See `test_frame_codec.py` for the policy in use.
"""

import base64
import gzip
import os
import zlib
from pathlib import Path
from typing import Any, cast

import pytest

from rmote.protocol import BaseProtocol, Flags


class Packets(BaseProtocol):
    """Protocol that compresses each packet, without the frame codec."""

    FRAME_CODEC = False


def protocol() -> BaseProtocol:
    return Packets(cast(Any, None), cast(Any, None))


def test_the_level_is_not_the_slowest_one():
    # gzip uses 9 by default, which costs several times more for a fraction of
    # a percent of the bytes.
    assert 1 <= BaseProtocol.COMPRESSION_LEVEL <= 6


def test_text_still_compresses_well():
    payload = (b"rmote carries source and facts " * 4096)[:131072]
    packed = protocol().compressed(payload)
    assert packed is not None
    assert len(payload) / len(packed) > 100


def test_random_bytes_are_not_compressed():
    assert protocol().compressed(os.urandom(131072)) is None


def test_already_compressed_bytes_are_not_compressed_again():
    once = zlib.compress(os.urandom(131072), 9)
    assert len(once) > BaseProtocol.COMPRESSION_SAMPLE
    assert protocol().compressed(once) is None


def test_byte_density_refuses_a_payload_that_cannot_shrink():
    instance = protocol()
    # Random and already compressed data use most of the byte values.
    assert instance.dense(os.urandom(4096))
    assert instance.dense(gzip.compress(os.urandom(16384)))
    # Text, source and structured data stay far below the limit.
    assert not instance.dense((b"rmote carries source and facts " * 64)[:4096])
    assert not instance.dense(Path("rmote/protocol.py").read_bytes()[:4096])
    assert not instance.dense(b'{"available": true, "total_bytes": 16360236032}' * 40)
    assert not instance.dense(base64.b64encode(os.urandom(4096)))


def test_a_sample_decides_for_the_whole_payload():
    instance = protocol()
    assert instance.worth_compressing(b"a" * instance.COMPRESSION_SAMPLE * 4)
    assert not instance.worth_compressing(os.urandom(instance.COMPRESSION_SAMPLE * 4))


def test_a_payload_below_the_sample_is_tried_in_full():
    instance = protocol()
    small = os.urandom(instance.COMPRESSION_SAMPLE // 2)
    # No sample is taken, and the full attempt is refused for not shrinking.
    assert instance.compressed(small) is None
    assert instance.compressed(b"a" * (instance.COMPRESSION_SAMPLE // 2)) is not None


@pytest.mark.asyncio
async def test_small_payloads_compress_without_a_worker_thread(monkeypatch):
    calls = []

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("a small payload must not pay for a hand-off")

    monkeypatch.setattr("rmote.protocol.asyncio.to_thread", forbidden)
    instance = protocol()
    packed = await instance.pack(b"a" * (instance.COMPRESSION_INLINE // 2))
    assert packed is not None
    assert calls == []
    assert await instance.unpack(packed) == b"a" * (instance.COMPRESSION_INLINE // 2)


@pytest.mark.asyncio
async def test_large_payloads_keep_the_loop_free(monkeypatch):
    seen = []
    original = __import__("asyncio").to_thread

    async def tracked(function: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(function)
        return await original(function, *args, **kwargs)

    monkeypatch.setattr("rmote.protocol.asyncio.to_thread", tracked)
    instance = protocol()
    payload = b"a" * (instance.COMPRESSION_INLINE + 1)
    packed = await instance.pack(payload)
    assert packed is not None
    # One hand-off: the pass over 256 KiB pays for it, and the resulting frame
    # is small enough to expand on the loop.
    assert len(seen) == 1
    assert await instance.unpack(packed) == payload
    assert len(seen) == 1
    # A frame above the decompression limit expands in a thread instead.
    random = os.urandom(131072)
    assert await instance.unpack(gzip.compress(random)) == random
    assert len(seen) == 2


class Recorder:
    """Collect the frames a protocol writes, without a transport."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    def is_closing(self) -> bool:
        return False

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "compressed"),
    [
        pytest.param(b"a" * 512, False, id="text-512b"),
        pytest.param(b"a" * 4096, True, id="text-4kib"),
        pytest.param(os.urandom(4096), False, id="random-4kib"),
        pytest.param(os.urandom(1 << 20), False, id="random-1mib"),
        pytest.param((b"rmote " * 200000)[: 1 << 20], True, id="text-1mib"),
    ],
)
async def test_the_flag_states_what_the_sender_decided(payload, compressed):
    writer = Recorder()
    instance = Packets(cast(Any, None), cast(Any, writer))
    await instance.send_serialized(payload, Flags.RPC, 1)
    flags = Flags(BaseProtocol.PACKET_HEADER.unpack(writer.frames[0][: BaseProtocol.PACKET_HEADER.size])[1])
    assert bool(flags & Flags.COMPRESSED) is compressed


@pytest.mark.asyncio
async def test_a_packet_never_grows_on_the_wire():
    for payload in (os.urandom(4096), os.urandom(1 << 20), zlib.compress(os.urandom(1 << 18))):
        writer = Recorder()
        instance = Packets(cast(Any, None), cast(Any, writer))
        await instance.send_serialized(payload, Flags.RPC, 1)
        sent = sum(len(frame) for frame in writer.frames)
        overhead = len(writer.frames) * BaseProtocol.PACKET_HEADER.size
        assert sent - overhead <= len(payload)
