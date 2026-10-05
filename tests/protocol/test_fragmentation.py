"""Frame fragmentation tests for BaseProtocol.

Fragmentation is a transport feature. These tests use the wire format directly
and do not depend on the shell or on any Tool.
"""

import asyncio
import os
import pickle

import pytest

from rmote.protocol import BaseProtocol, Flags, FragmentBuffer, Protocol, Tool


class CollectingWriter:
    """Minimal StreamWriter stand-in that keeps every written frame."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.frames.append(bytes(data))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


def decode(frame: bytes) -> tuple[Flags, int, bytes]:
    """Split one frame into flags, packet_id and payload."""
    size = BaseProtocol.PACKET_HEADER.size
    magic, raw_flags, length, packet_id = BaseProtocol.PACKET_HEADER.unpack(frame[:size])
    assert magic == BaseProtocol.MAGIC
    payload = frame[size:]
    assert len(payload) == length
    return Flags(raw_flags), packet_id, payload


def encode(flags: Flags, packet_id: int, payload: bytes) -> bytes:
    """Build one frame from flags, packet_id and payload."""
    header = BaseProtocol.PACKET_HEADER.pack(BaseProtocol.MAGIC, int(flags), len(payload), packet_id)
    return header + payload


class Packets(BaseProtocol):
    """Protocol that compresses each packet, without the frame codec."""

    FRAME_CODEC = False


def make_protocol(
    data: bytes = b"", policy: type[BaseProtocol] = BaseProtocol
) -> tuple[BaseProtocol, CollectingWriter]:
    reader = asyncio.StreamReader()
    if data:
        reader.feed_data(data)
    reader.feed_eof()
    writer = CollectingWriter()
    return policy(reader, writer), writer  # type: ignore[arg-type]


class TestSendFragmentation:
    @pytest.mark.asyncio
    async def test_small_packet_stays_one_frame(self) -> None:
        proto, writer = make_protocol()
        await proto.send({"small": True}, Flags.RPC | Flags.REQUEST, 7)

        assert len(writer.frames) == 1
        flags, packet_id, _ = decode(writer.frames[0])
        assert packet_id == 7
        assert not flags & Flags.FRAGMENT

    @pytest.mark.asyncio
    async def test_large_packet_splits_and_marks_all_but_last(self) -> None:
        proto, writer = make_protocol()
        # Random bytes do not compress, so the payload stays above FRAGMENT_SIZE.
        packet = os.urandom(proto.FRAGMENT_SIZE * 3 + 100)
        await proto.send(packet, Flags.RPC | Flags.REQUEST, 11)

        assert len(writer.frames) >= 4
        decoded = [decode(frame) for frame in writer.frames]

        for flags, packet_id, payload in decoded[:-1]:
            assert flags & Flags.FRAGMENT
            assert packet_id == 11
            assert len(payload) == proto.FRAGMENT_SIZE

        last_flags, last_id, _ = decoded[-1]
        assert not last_flags & Flags.FRAGMENT
        assert last_id == 11

    @pytest.mark.asyncio
    async def test_logical_flags_repeat_on_every_fragment(self) -> None:
        proto, writer = make_protocol()
        await proto.send(b"repeat " * proto.FRAGMENT_SIZE, Flags.RPC | Flags.RESPONSE, 3)

        assert len(writer.frames) > 1
        for frame in writer.frames:
            flags, _, _ = decode(frame)
            assert flags & Flags.RPC
            assert flags & Flags.RESPONSE

    @pytest.mark.asyncio
    async def test_the_packet_policy_marks_every_fragment_of_one_packet(self) -> None:
        """The packet policy compresses the payload, so each fragment says so."""
        proto, writer = make_protocol(policy=Packets)
        # The payload has to compress, because only then does the COMPRESSED
        # flag exist to repeat. Random bytes are sent as they are.
        await proto.send(b"repeat " * proto.FRAGMENT_SIZE, Flags.RPC | Flags.RESPONSE, 3)

        for frame in writer.frames:
            flags, _, _ = decode(frame)
            # Compression describes the whole packet, not one fragment.
            assert flags & Flags.COMPRESSED

    @pytest.mark.asyncio
    async def test_payload_of_exactly_fragment_size_is_one_frame(self) -> None:
        proto, writer = make_protocol()
        await proto.send_payload(b"x" * proto.FRAGMENT_SIZE, Flags.RPC, 5)

        assert len(writer.frames) == 1
        flags, _, payload = decode(writer.frames[0])
        assert not flags & Flags.FRAGMENT
        assert len(payload) == proto.FRAGMENT_SIZE

    @pytest.mark.asyncio
    async def test_empty_payload_is_one_frame(self) -> None:
        proto, writer = make_protocol()
        await proto.send_payload(b"", Flags.RPC, 9)

        assert len(writer.frames) == 1
        flags, _, payload = decode(writer.frames[0])
        assert not flags & Flags.FRAGMENT
        assert payload == b""

    @pytest.mark.asyncio
    async def test_send_rejects_fragment_flag(self) -> None:
        proto, _ = make_protocol()

        with pytest.raises(ValueError, match="Fragment flag must not be set"):
            await proto.send({"a": 1}, Flags.FRAGMENT, 1)


class TestReceiveReassembly:
    @pytest.mark.asyncio
    async def test_roundtrip_through_fragments(self) -> None:
        sender, writer = make_protocol()
        packet = os.urandom(sender.FRAGMENT_SIZE * 2 + 7)
        await sender.send(packet, Flags.RPC | Flags.REQUEST, 21)

        receiver, _ = make_protocol(b"".join(writer.frames))
        received = await receiver.receive()

        assert received.payload == packet
        assert received.packet_id == 21
        assert not received.flags & Flags.FRAGMENT
        # The size is the serialized length before compression.
        assert received.size == len(pickle.dumps(packet))
        # The buffer is released after the packet is complete.
        assert receiver.fragments.buffers == {}
        assert receiver.fragments.total_bytes == 0

    @pytest.mark.asyncio
    async def test_interleaved_packets_complete_independently(self) -> None:
        first = pickle.dumps({"id": 1})
        second = pickle.dumps({"id": 2})

        wire = b"".join(
            [
                encode(Flags.RPC | Flags.FRAGMENT, 1, first[:3]),
                encode(Flags.RPC | Flags.FRAGMENT, 2, second[:3]),
                encode(Flags.RPC, 2, second[3:]),
                encode(Flags.RPC, 1, first[3:]),
            ]
        )
        receiver, _ = make_protocol(wire)

        # Packet 2 completes first, so it is returned first.
        second_packet = await receiver.receive()
        first_packet = await receiver.receive()

        assert (second_packet.payload, second_packet.flags, second_packet.packet_id) == ({"id": 2}, Flags.RPC, 2)
        assert (first_packet.payload, first_packet.flags, first_packet.packet_id) == ({"id": 1}, Flags.RPC, 1)
        assert receiver.fragments.buffers == {}
        assert receiver.fragments.total_bytes == 0

    @pytest.mark.asyncio
    async def test_unfragmented_packet_ignores_other_buffers(self) -> None:
        pending = pickle.dumps({"pending": True})
        whole = pickle.dumps("whole")

        wire = encode(Flags.RPC | Flags.FRAGMENT, 1, pending[:4]) + encode(Flags.RPC, 2, whole)
        receiver, _ = make_protocol(wire)

        received = await receiver.receive()
        assert (received.payload, received.flags, received.packet_id) == ("whole", Flags.RPC, 2)
        # The incomplete packet is still buffered.
        assert set(receiver.fragments.buffers) == {1}
        assert receiver.fragments.total_bytes == 4


class TestFragmentBuffer:
    def test_take_without_stored_fragments_returns_tail(self) -> None:
        buffer = FragmentBuffer(max_bytes=1024, max_packets=4)

        assert buffer.take(1, b"whole") == b"whole"
        assert buffer.buffers == {}
        assert buffer.total_bytes == 0

    def test_store_then_take_joins_in_order(self) -> None:
        buffer = FragmentBuffer(max_bytes=1024, max_packets=4)
        buffer.store(1, b"a")
        buffer.store(1, b"b")

        assert buffer.total_bytes == 2
        assert buffer.take(1, b"c") == b"abc"
        assert buffer.total_bytes == 0

    def test_taking_one_packet_releases_only_its_bytes(self) -> None:
        """The budget follows the bytes of each packet, not the frame count."""
        buffer = FragmentBuffer(max_bytes=1024, max_packets=4)
        buffer.store(1, b"one-")
        buffer.store(1, b"more-")
        buffer.store(2, b"two-")

        assert buffer.total_bytes == 13
        assert buffer.take(1, b"end") == b"one-more-end"
        assert buffer.total_bytes == 4
        assert buffer.take(2, b"end") == b"two-end"
        assert buffer.total_bytes == 0

    def test_packets_stay_separate(self) -> None:
        buffer = FragmentBuffer(max_bytes=1024, max_packets=4)
        buffer.store(1, b"one-")
        buffer.store(2, b"two-")

        assert buffer.take(2, b"end") == b"two-end"
        assert buffer.take(1, b"end") == b"one-end"

    def test_max_packets_counts_distinct_ids(self) -> None:
        buffer = FragmentBuffer(max_bytes=1024, max_packets=2)
        buffer.store(1, b"x")
        buffer.store(2, b"x")
        # More fragments for a known id stay allowed.
        buffer.store(1, b"y")

        with pytest.raises(ValueError, match="Too many incomplete packets: 3"):
            buffer.store(3, b"x")

    def test_max_bytes_counts_every_packet(self) -> None:
        buffer = FragmentBuffer(max_bytes=6, max_packets=4)
        buffer.store(1, b"abc")
        buffer.store(2, b"abc")

        with pytest.raises(ValueError, match="Reassembly buffer overflow: 7 bytes"):
            buffer.store(1, b"d")

    def test_clear_releases_everything(self) -> None:
        buffer = FragmentBuffer(max_bytes=1024, max_packets=4)
        buffer.store(1, b"abc")
        buffer.clear()

        assert buffer.buffers == {}
        assert buffer.total_bytes == 0


class TestReassemblyLimits:
    @pytest.mark.asyncio
    async def test_too_many_incomplete_packets(self) -> None:
        class Limited(BaseProtocol):
            MAX_PARTIAL_PACKETS = 2

        wire = b"".join(encode(Flags.RPC | Flags.FRAGMENT, i, b"x") for i in range(3))
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        proto = Limited(reader, CollectingWriter())  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Too many incomplete packets: 3"):
            await proto.receive()

    @pytest.mark.asyncio
    async def test_reassembly_buffer_overflow(self) -> None:
        class Limited(BaseProtocol):
            MAX_REASSEMBLY_BYTES = 8

        wire = encode(Flags.RPC | Flags.FRAGMENT, 1, b"x" * 5) + encode(Flags.RPC | Flags.FRAGMENT, 1, b"x" * 5)
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        proto = Limited(reader, CollectingWriter())  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Reassembly buffer overflow: 10 bytes"):
            await proto.receive()


class BigPayloadTool(Tool):
    @staticmethod
    def echo_big(size: int) -> bytes:
        import os as _os

        return _os.urandom(size)

    @staticmethod
    def measure(data: bytes) -> int:
        return len(data)


class TestFragmentationOverRealPipe:
    @pytest.mark.asyncio
    async def test_large_result_survives_fragmentation(self, protocol: Protocol) -> None:
        size = BaseProtocol.FRAGMENT_SIZE * 3 + 13
        data = await protocol(BigPayloadTool.echo_big, size)

        assert isinstance(data, bytes)
        assert len(data) == size

    @pytest.mark.asyncio
    async def test_large_argument_survives_fragmentation(self, protocol: Protocol) -> None:
        payload = os.urandom(BaseProtocol.FRAGMENT_SIZE * 2 + 5)
        assert await protocol(BigPayloadTool.measure, payload) == len(payload)

    @pytest.mark.asyncio
    async def test_concurrent_large_calls_interleave(self, protocol: Protocol) -> None:
        size = BaseProtocol.FRAGMENT_SIZE * 2 + 1
        results = await asyncio.gather(*(protocol(BigPayloadTool.echo_big, size) for _ in range(4)))

        assert [len(item) for item in results] == [size] * 4
        # Independent random payloads must not be mixed up during reassembly.
        assert len(set(results)) == 4
