"""Streaming call tests.

A tool method written as an async generator pushes its items without a request
per item. The items that are ready together travel in one packet, and an item
that is alone travels at once.
"""

import asyncio
import contextlib
import pickle
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rmote.protocol import Flags, Protocol, StreamCredit
from tests.support.synchronization import ObservedEvent, wait_blocked
from tests.support.tool_cases.streaming import Streamer, Waiting


class Counting(Protocol):
    """Count the packets that carry stream items, and the items in them."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.packets = 0
        self.items = 0

    async def receive(self):
        packet = await super().receive()
        if packet.flags & Flags.STREAM and packet.flags & Flags.RESPONSE:
            self.packets += 1
            self.items += len(packet.payload) if packet.flags & Flags.BATCH else 1
        return packet


class TestStreamingCalls:
    @pytest.mark.asyncio
    async def test_items_arrive_in_order(self, protocol: Protocol) -> None:
        received = [item async for item in protocol(Streamer.counted, 50)]

        assert received == list(range(50))

    @pytest.mark.asyncio
    async def test_empty_stream_ends_at_once(self, protocol: Protocol) -> None:
        assert [item async for item in protocol(Streamer.counted, 0)] == []

    @pytest.mark.asyncio
    async def test_large_items_survive_fragmentation(self, protocol: Protocol) -> None:
        size = protocol.FRAGMENT_SIZE * 2 + 17
        received = [item async for item in protocol(Streamer.chunks, 3, size)]

        assert [len(item) for item in received] == [size] * 3
        assert received[0][:1] == b"\x00"
        assert received[1][:1] == b"\x01"
        assert len(set(received)) == 3

    @pytest.mark.asyncio
    async def test_items_above_twice_the_window_go_alone(self, protocol: Protocol) -> None:
        size = 9 * 1024 * 1024
        received = []
        async for item in protocol(Streamer.chunks, 3, size):
            received.append((len(item), item[:1]))
            assert await protocol(Streamer.add, 2, 3) == 5
        assert received == [(size, bytes((i,))) for i in range(3)]

    @pytest.mark.parametrize("sizes", [(101, 1), (1, 101), (201,), (60, 60)])
    def test_receiver_rejects_items_outside_credit(self, sizes: tuple[int, ...]) -> None:
        proto = Protocol.__new__(Protocol)
        proto.MAX_STREAM_WINDOW = 100
        proto.MAX_STREAM_ITEM = 200
        proto.streams = {7: asyncio.Queue(maxsize=Protocol.MAX_STREAM_BACKLOG + 1)}
        proto.streams_over_budget = set()
        proto.stream_buffered = {}
        for size in sizes:
            proto.handle_stream_item(Flags.RPC | Flags.RESPONSE | Flags.STREAM, 7, b"x", size)
        assert 7 in proto.streams_over_budget

    @pytest.mark.asyncio
    async def test_ordinary_call_progresses_while_a_stream_is_open(self, protocol: Protocol) -> None:
        """Fragmentation keeps one long stream from holding the channel."""
        items: list[int] = []
        async for item in protocol(Streamer.chunks, 4, protocol.FRAGMENT_SIZE):
            if not items:
                assert await protocol(Streamer.add, 2, 3) == 5
            items.append(len(item))

        assert items == [protocol.FRAGMENT_SIZE] * 4

    @pytest.mark.asyncio
    async def test_failure_reaches_the_consumer_after_the_items(self, protocol: Protocol) -> None:
        received: list[str] = []

        with pytest.raises(Exception, match="generator failed"):
            async for item in protocol(Streamer.failing):
                received.append(item)

        assert received == ["first"]

    @pytest.mark.asyncio
    async def test_leaving_early_keeps_the_connection(self, protocol: Protocol) -> None:
        async for item in protocol(Streamer.counted, 1000):
            if item == 3:
                break

        # The registration is released, and ordinary calls still work.
        assert await protocol(Streamer.add, 1, 1) == 2

    @pytest.mark.asyncio
    async def test_two_streams_run_at_once(self, protocol: Protocol) -> None:
        async def collect(count: int) -> list[int]:
            return [item async for item in protocol(Streamer.paired, count)]

        first, second = await asyncio.gather(collect(10), collect(10))

        assert first == list(range(10))
        assert second == list(range(10))

    @pytest.mark.asyncio
    async def test_a_slow_consumer_slows_the_sender(self, client, monkeypatch) -> None:
        """Permission bounds the items in flight, so memory stays bounded.

        The sender stops by itself while nothing is consumed, and every item
        still arrives once reading continues.
        """
        credit = StreamCredit(max_bytes=1_000_000, max_items=2)
        credit.ready = ready = ObservedEvent()
        monkeypatch.setattr("rmote.protocol.StreamCredit", lambda *args: credit)
        produced = []
        received = []

        async def produce() -> AsyncIterator[int]:
            for number in range(7):
                produced.append(number)
                yield number

        async def collect(payload, flags, packet_id):
            received.extend(Protocol.split(payload) if flags & Flags.BATCH else [pickle.loads(payload)])

        monkeypatch.setattr(client, "send_serialized", collect)
        sending = asyncio.create_task(client.send_stream(produce(), 1))
        try:
            for blocked_at in (3, 5, 7):
                await wait_blocked(sending, ready.entered)
                # Two items per credit grant, plus one read ahead.
                assert produced == list(range(blocked_at))
                assert credit.items_in_flight == 2
                assert not sending.done()
                ready.entered.clear()
                credit.give_back(credit.bytes_in_flight, 2)
            await sending
            assert received == list(range(7))
        finally:
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)

    def test_a_peer_that_ignores_permission_ends_the_stream(self) -> None:
        """The receive side keeps a last defence against a faulty peer.

        The permission already bounds the backlog, so this only triggers when a
        peer sends more than it was allowed.
        """
        proto = Protocol.__new__(Protocol)
        proto.streams = {}
        proto.streams_over_budget = set()
        proto.stream_buffered = {}
        queue: asyncio.Queue[tuple[Flags, object, int]] = asyncio.Queue(maxsize=Protocol.MAX_STREAM_BACKLOG + 1)
        proto.streams[7] = queue

        item_flags = Flags.RPC | Flags.RESPONSE | Flags.STREAM
        for number in range(Protocol.MAX_STREAM_BACKLOG + 5):
            proto.handle_stream_item(item_flags, 7, number, 8)

        collected = []
        while not queue.empty():
            collected.append(queue.get_nowait())

        assert len(collected) == Protocol.MAX_STREAM_BACKLOG + 1
        last_flags, payload, _ = collected[-1]
        assert last_flags & Flags.EXCEPTION
        assert isinstance(payload, RuntimeError)
        assert "ignored its permission" in str(payload)

    def test_a_peer_above_the_byte_budget_ends_the_stream(self) -> None:
        """A few huge items pass the count limit but not the byte budget."""
        proto = Protocol.__new__(Protocol)
        proto.streams = {}
        proto.streams_over_budget = set()
        proto.stream_buffered = {}
        queue: asyncio.Queue[tuple[Flags, object, int]] = asyncio.Queue(maxsize=Protocol.MAX_STREAM_BACKLOG + 1)
        proto.streams[9] = queue

        item_flags = Flags.RPC | Flags.RESPONSE | Flags.STREAM
        huge = Protocol.MAX_STREAM_WINDOW
        for number in range(4):
            proto.handle_stream_item(item_flags, 9, number, huge)

        collected = []
        while not queue.empty():
            collected.append(queue.get_nowait())

        last_flags, payload, _ = collected[-1]
        assert last_flags & Flags.EXCEPTION
        assert isinstance(payload, RuntimeError)
        assert "bytes are waiting" in str(payload)
        # The count limit was never reached, so the byte budget stopped it.
        assert len(collected) < Protocol.MAX_STREAM_BACKLOG

    @pytest.mark.asyncio
    async def test_stream_is_not_started_before_it_is_read(self, protocol: Protocol) -> None:
        """An async generator is lazy, so no request goes out on its own."""
        items = protocol.stream(Streamer.counted, 5)
        assert protocol.streams == {}
        assert await items.__anext__() == 0
        await items.aclose()


class TestStreamingAfterDisconnect:
    @pytest.mark.asyncio
    async def test_lost_connection_reaches_the_consumer(self) -> None:
        import sys

        proto = await Protocol.from_command(python=sys.executable)
        async with proto:
            items = proto(Streamer.gated)
            assert await items.__anext__() == 0

            assert proto._owned_process is not None
            proto._owned_process.kill()

            with pytest.raises((ConnectionError, EOFError, asyncio.IncompleteReadError)):
                async for _ in items:
                    pass


class TestNativeCall:
    @pytest.mark.asyncio
    async def test_a_generator_method_gives_an_async_iterator(self, protocol: Protocol) -> None:
        """No await is needed, because the call returns the iterator itself."""
        items = protocol(Streamer.counted, 3)

        assert hasattr(items, "__anext__")
        assert [item async for item in items] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_an_ordinary_method_still_needs_await(self, protocol: Protocol) -> None:
        assert await protocol(Streamer.add, 40, 2) == 42

    @pytest.mark.asyncio
    async def test_explicit_stream_matches_the_native_call(self, protocol: Protocol) -> None:
        native = [item async for item in protocol(Streamer.counted, 5)]
        explicit = [item async for item in protocol.stream(Streamer.counted, 5)]

        assert native == explicit == [0, 1, 2, 3, 4]


class TestCreditAccounting:
    """Unit checks of the permission itself."""

    @pytest.mark.asyncio
    async def test_items_fit_until_the_budget_is_spent(self) -> None:
        credit = StreamCredit(max_bytes=100, max_items=10)

        assert await credit.spend(60)
        assert credit.bytes_in_flight == 60
        assert credit.fits(40)
        assert not credit.fits(41)

    @pytest.mark.asyncio
    async def test_an_item_above_the_budget_goes_alone(self) -> None:
        """An item larger than the budget must not wait for room forever."""
        credit = StreamCredit(max_bytes=100, max_items=10)

        assert await credit.spend(500)
        assert credit.bytes_in_flight == 500
        # Nothing else fits while it is in flight.
        assert not credit.fits(1)

    @pytest.mark.asyncio
    async def test_the_count_limit_holds_back_small_items(self) -> None:
        credit = StreamCredit(max_bytes=1_000_000, max_items=2)

        assert await credit.spend(1)
        assert await credit.spend(1)
        assert not credit.fits(1)

    @pytest.mark.asyncio
    async def test_a_return_above_what_is_in_flight_is_refused(self) -> None:
        credit = StreamCredit(max_bytes=100, max_items=10)
        await credit.spend(10)

        with pytest.raises(ValueError, match="above what is in flight"):
            credit.give_back(50, 1)

    @pytest.mark.asyncio
    async def test_a_negative_return_is_refused(self) -> None:
        credit = StreamCredit(max_bytes=100, max_items=10)

        with pytest.raises(ValueError, match="Negative stream permission"):
            credit.give_back(-1, 0)

    @pytest.mark.asyncio
    async def test_release_stops_a_waiting_sender(self) -> None:
        credit = StreamCredit(max_bytes=10, max_items=10)
        credit.ready = ready = ObservedEvent()
        await credit.spend(10)

        waiting = asyncio.ensure_future(credit.spend(10))
        await wait_blocked(waiting, ready.entered)
        assert not waiting.done()

        credit.release()
        assert await waiting is False


class TestItemSizeLimit:
    @pytest.mark.asyncio
    async def test_an_item_above_the_limit_fails_the_stream(self) -> None:
        """A limit that cannot be met must give an error, not a wait."""
        proto = Protocol.__new__(Protocol)
        proto.stream_credits = {}
        proto.stream_senders = {}
        proto.known_modules = set()
        # The writer of a stream runs as a task of the connection.
        proto._tasks = set()
        proto.MAX_STREAM_ITEM = 200
        proto.MAX_STREAM_WINDOW = 1000
        proto.MAX_STREAM_BACKLOG = 8
        written: list[bytes] = []

        async def collect(payload: bytes, flags: Flags, packet_id: int) -> None:
            written.append(payload)

        proto.send_serialized = collect  # type: ignore[method-assign]

        async def items() -> AsyncIterator[bytes]:
            yield b"x" * 10
            yield b"y" * 1000

        with pytest.raises(ValueError, match="above the limit"):
            await proto.send_stream(items(), 3)

        assert len(written) == 1
        assert proto.stream_credits == {}


class TestFlowControlUnderLoad:
    @pytest.mark.asyncio
    async def test_a_stopped_consumer_does_not_stop_another_stream(self, protocol: Protocol) -> None:
        stalled = protocol(Streamer.counted, protocol.MAX_STREAM_BACKLOG * 4)
        assert await stalled.__anext__() == 0

        # The second stream runs to its end while the first one is not read.
        assert [item async for item in protocol(Streamer.counted, 20)] == list(range(20))
        assert await protocol(Streamer.add, 2, 2) == 4

        await stalled.aclose()

    @pytest.mark.asyncio
    async def test_compressible_items_keep_their_bytes(self, protocol: Protocol) -> None:
        """The budget counts the serialized size, not the compressed size."""
        size = protocol.COMPRESSION_THRESHOLD * 4
        received = [item async for item in protocol(Streamer.compressible, 5, size)]

        assert received == [b"a" * size] * 5

    @pytest.mark.asyncio
    async def test_explicit_close_releases_the_stream(self, protocol: Protocol) -> None:
        """aclosing closes the iterator at once, unlike a bare break."""
        async with contextlib.aclosing(protocol(Streamer.counted, 10_000)) as items:
            async for item in items:
                if item == 2:
                    break

        assert protocol.streams == {}
        assert await protocol(Streamer.add, 3, 4) == 7

    @pytest.mark.asyncio
    async def test_cancelling_the_consumer_releases_the_stream(self, protocol: Protocol) -> None:
        received = asyncio.Event()

        async def read_forever() -> None:
            async for _ in protocol(Streamer.gated):
                received.set()

        task = asyncio.ensure_future(read_forever())
        await received.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await protocol(Streamer.add, 5, 5) == 10

    @pytest.mark.asyncio
    async def test_file_blocks_reach_the_writer(self, protocol: Protocol, tmp_path: Path) -> None:
        """A file tool yields bounded blocks without losing data."""
        source = tmp_path / "source.bin"
        payload = bytes(range(256)) * 4000
        source.write_bytes(payload)

        target = tmp_path / "target.bin"
        with target.open("wb") as handle:
            async for block in protocol(Streamer.blocks, str(source), 64 * 1024):
                handle.write(block)

        assert target.read_bytes() == payload


class TestBatches:
    @pytest.mark.asyncio
    async def test_a_producer_that_does_not_wait_fills_one_packet(self) -> None:
        """Items that are ready together cost one write and one read."""
        count = 500
        async with await Counting.from_command(python=sys.executable) as remote:
            received = [item async for item in remote.stream(Streamer.counted, count)]

        assert received == list(range(count))
        assert remote.items == count
        # One packet per item would be 500. The producer never waits, so the
        # buffer fills while a packet is on the wire.
        assert remote.packets <= count // 10

    @pytest.mark.asyncio
    async def test_a_producer_that_waits_does_not_hold_its_item(self, protocol: Protocol) -> None:
        """An item that is alone travels at once, so batching adds no delay."""
        async with asyncio.timeout(5), contextlib.aclosing(protocol.stream(Streamer.gated)) as items:
            assert await anext(items) == 0
            # The next item cannot exist until the consumer receives the first.
            await protocol(Streamer.release)
            assert await anext(items) == 1
            with pytest.raises(StopAsyncIteration):
                await anext(items)

    def test_a_batch_round_trips_in_order(self) -> None:
        values = [1, "two", b"three", [4], {"five": 5}]
        frames = [pickle.dumps(value) for value in values]

        assert Protocol.split(Protocol.batch(frames)) == values
        assert Protocol.split(Protocol.batch([])) == []

    @pytest.mark.parametrize("payload", [b"\x00\x00", b"\x00\x00\x00\x05abc"])
    def test_a_broken_batch_is_refused(self, payload: bytes) -> None:
        with pytest.raises(ValueError, match="Truncated"):
            Protocol.split(payload)


class TestLeavingAWaitingStream:
    @pytest.mark.asyncio
    async def test_the_cleanup_of_a_waiting_generator_runs(self, protocol: Protocol, tmp_path: Path) -> None:
        """A consumer that leaves cancels the producer, whatever it waits for."""
        marker = str(tmp_path / "closed")
        async with contextlib.aclosing(protocol(Waiting.forever, marker)) as items:
            assert await items.__anext__() == 1

        await protocol(Waiting.wait_closed)
        assert await protocol(Waiting.cleaned, marker)
        # The connection is intact, so an ordinary call still answers.
        assert await protocol(Streamer.add, 2, 3) == 5

    @pytest.mark.asyncio
    async def test_a_break_also_ends_a_waiting_generator(self, protocol: Protocol, tmp_path: Path) -> None:
        """A bare break closes the iterator when it is collected, then cancels."""
        marker = str(tmp_path / "closed")
        async for _ in protocol(Waiting.forever, marker):
            break

        await protocol(Waiting.wait_closed)
        assert await protocol(Waiting.cleaned, marker)

    @pytest.mark.asyncio
    async def test_other_streams_keep_running(self, protocol: Protocol, tmp_path: Path) -> None:
        """Cancelling one producer leaves the connection and its streams alone."""
        marker = str(tmp_path / "closed")
        other = protocol(Streamer.gated)
        assert await other.__anext__() == 0

        async with contextlib.aclosing(protocol(Waiting.forever, marker)) as items:
            assert await items.__anext__() == 1

        await protocol(Waiting.wait_closed)
        await protocol(Streamer.release)
        assert [item async for item in other] == [1]
        assert await protocol(Waiting.cleaned, marker)
