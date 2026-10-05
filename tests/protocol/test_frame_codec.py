"""The frame codec compresses bodies against the history of the connection.

One dictionary serves each direction. A body that shrinks carries the mark and
moves the dictionary; a body that does not shrink travels raw and leaves it
alone. The peer therefore inflates exactly the bodies that were deflated.
"""

import asyncio
import os
import sys
import zlib
from typing import Any, cast

import pytest

from rmote.protocol import BaseProtocol, Flags, FrameCompressor, FrameDecompressor, Protocol
from tests.support.tool_cases.compression_probe import CompressionProbe as Probe

TEXT = b'{"host": "example", "available": true, "total_bytes": 16360236032}'


def compressor() -> FrameCompressor:
    return FrameCompressor(BaseProtocol.COMPRESSION_LEVEL, BaseProtocol.DENSITY_SAMPLE, BaseProtocol.DENSITY_LIMIT)


def test_the_dictionary_holds_the_history_of_the_direction():
    codec = compressor()
    first, packed = codec.encode(TEXT * 16)
    assert packed
    second, packed = codec.encode(TEXT * 16)
    assert packed
    # The second body repeats the first, so it carries a reference to it.
    assert len(second) < len(first) / 4


def test_a_body_that_cannot_shrink_travels_raw():
    codec = compressor()
    random = os.urandom(4096)
    body, packed = codec.encode(random)
    assert body is random and not packed


def test_a_raw_body_leaves_the_dictionary_alone():
    with_random = compressor()
    without = compressor()
    for codec in (with_random, without):
        codec.encode(TEXT * 16)
    with_random.encode(os.urandom(4096))
    # The random body never entered the dictionary, so the next body of both
    # compressors is the same bytes.
    assert with_random.encode(TEXT * 16) == without.encode(TEXT * 16)


def test_a_body_that_passes_the_sample_and_cannot_shrink_grows_a_little():
    """No trial runs, so such a body travels compressed and costs a few bytes.

    A copy of the dictionary would cost ten microseconds of its window, which
    is more than the pass it would test.
    """
    codec = compressor()
    # Four random bytes use four byte values, so the sample lets them through.
    body, packed = codec.encode(os.urandom(4))
    assert packed
    assert len(body) < 32
    # The dictionary holds them now, and the decoder follows it.
    assert len(FrameDecompressor(BaseProtocol.FRAGMENT_SIZE).decode(body)) == 4


def test_a_tool_can_refuse_compression():
    codec = compressor()
    body, packed = codec.encode(TEXT * 16, allowed=False)
    assert body is not None and not packed
    # The refusal keeps the dictionary empty, so a later body starts fresh.
    assert codec.encode(TEXT * 16) == compressor().encode(TEXT * 16)


def test_the_peer_follows_the_dictionary():
    codec = compressor()
    decoder = FrameDecompressor(BaseProtocol.FRAGMENT_SIZE)
    bodies = [TEXT * 16, os.urandom(2048), TEXT * 16, b"a" * 4096, b"", TEXT * 32]
    for body in bodies:
        wire, packed = codec.encode(body)
        assert decoder.decode(wire) == body if packed else wire == body


def test_a_body_without_a_flush_boundary_is_refused():
    decoder = FrameDecompressor(BaseProtocol.FRAGMENT_SIZE)
    with pytest.raises(ValueError, match="flush boundary"):
        decoder.decode(zlib.compress(TEXT))


def test_a_body_above_the_frame_limit_is_refused():
    limit = 1024
    codec = compressor()
    wire, packed = codec.encode(b"a" * (limit + 1))
    assert packed
    with pytest.raises(ValueError, match="above the frame limit"):
        FrameDecompressor(limit).decode(wire)


def test_a_corrupt_body_is_refused():
    codec = compressor()
    wire, packed = codec.encode(TEXT * 16)
    assert packed
    broken = bytearray(wire)
    broken[2] ^= 0xFF
    with pytest.raises((ValueError, zlib.error)):
        FrameDecompressor(BaseProtocol.FRAGMENT_SIZE).decode(bytes(broken))


@pytest.mark.asyncio
async def test_the_boundary_opens_the_codec_of_each_direction():
    remote = await Protocol.from_command(python=sys.executable)
    assert remote.deflate is None and remote.inflate is None
    async with remote:
        # The bootstrap and the boundary travel plain; the codecs serve what
        # follows them.
        assert isinstance(remote.deflate, FrameCompressor)
        assert isinstance(remote.inflate, FrameDecompressor)
        assert await remote(Probe.echo, b"x") == b"x"


@pytest.mark.asyncio
async def test_a_repeated_request_costs_less_every_time():
    """The frames of the same bytes shrink as the dictionary learns them."""
    async with await Protocol.from_command(python=sys.executable) as remote:
        await remote(Probe.echo, b"warm")
        sizes: list[int] = []
        original = remote.writer.write

        def counted(data: Any) -> None:
            sizes.append(len(data))
            original(data)

        remote.writer.write = counted  # type: ignore[method-assign]
        try:
            for _ in range(4):
                await remote(Probe.echo, TEXT * 64)
        finally:
            remote.writer.write = original  # type: ignore[method-assign]

    # Every request carries the same bytes, so the dictionary answers for them:
    # a frame of 4 KiB of repeated text costs about a hundred bytes.
    assert len(sizes) == 4
    assert max(sizes) < len(TEXT * 64) / 10
    assert sizes[-1] <= sizes[0]


@pytest.mark.asyncio
async def test_random_bytes_keep_their_size_and_a_tool_can_say_so():
    payload = os.urandom(64 * 1024)
    async with await Protocol.from_command(python=sys.executable) as remote:
        assert await remote(Probe.echo, payload) == payload
        assert await remote.uncompressed(Probe.echo, payload) == payload
        # The dictionary still works for text after the random bytes passed it.
        assert await remote(Probe.echo, TEXT * 64) == TEXT * 64


@pytest.mark.asyncio
async def test_a_frame_above_the_limit_is_refused_on_arrival():
    """A length that no sender of this protocol writes means a broken peer."""
    reader = asyncio.StreamReader()
    reader.feed_data(BaseProtocol.PACKET_HEADER.pack(BaseProtocol.MAGIC, Flags.RPC, BaseProtocol.FRAGMENT_SIZE + 1, 1))
    reader.feed_eof()
    instance = BaseProtocol(reader, cast(Any, None))
    with pytest.raises(ValueError, match="above the limit"):
        await instance.receive()
