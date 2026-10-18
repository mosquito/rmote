"""Decoding preserves wire order while large bodies leave the event loop."""

import asyncio
import pickle
import sys
import threading
from typing import Any, cast

import pytest

from rmote.protocol import BaseProtocol, Flags, FrameCompressor, FrameDecompressor, ModuleLoader, ModuleSource


def receiver(payloads: list[tuple[bytes, Flags]], *, compressed: bool = True) -> BaseProtocol:
    reader = asyncio.StreamReader()
    encoder = FrameCompressor(3, 256, 128)
    for packet_id, (payload, flags) in enumerate(payloads):
        for offset in range(0, len(payload), BaseProtocol.FRAGMENT_SIZE):
            chunk = payload[offset : offset + BaseProtocol.FRAGMENT_SIZE]
            body, packed = encoder.encode(chunk, allowed=compressed)
            frame_flags = flags | (Flags.COMPRESSED if packed else Flags(0))
            if offset + len(chunk) < len(payload):
                frame_flags |= Flags.FRAGMENT
            reader.feed_data(BaseProtocol.PACKET_HEADER.pack(BaseProtocol.MAGIC, frame_flags, len(body), packet_id))
            reader.feed_data(body)
    reader.feed_eof()
    instance = BaseProtocol(reader, cast(Any, None))
    instance.inflate = FrameDecompressor(instance.FRAGMENT_SIZE)
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False, True], ids=["raw", "zlib"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_large_payloads_decode_in_workers_in_wire_order(monkeypatch, compressed, batch):
    loop_thread = threading.get_ident()
    values = [
        [{"index": index, "text": str(index) + "journal message " * 200} for index in range(128)] for _ in range(2)
    ]
    flags = Flags.RPC | Flags.RESPONSE
    if batch:
        flags |= Flags.BATCH | Flags.STREAM
        payloads = [BaseProtocol.batch([pickle.dumps(item) for item in value]) for value in values]
    else:
        payloads = [pickle.dumps(value) for value in values]
    instance = receiver([(payload, flags) for payload in payloads], compressed=compressed)
    # Distinct strings keep the ordinary pickle large as well as the batch.
    instance.DECODE_INLINE = 1024
    decoded_on: list[int] = []
    original = pickle.loads

    def loads(payload):
        decoded_on.append(threading.get_ident())
        return original(payload)

    monkeypatch.setattr(pickle, "loads", loads)
    packets = await asyncio.gather(instance.receive(), instance.receive())
    assert [packet.payload for packet in packets] == values
    assert [packet.packet_id for packet in packets] == [0, 1]
    assert [packet.size for packet in packets] == list(map(len, payloads))
    assert all(packet.flags == flags for packet in packets)
    assert decoded_on and all(ident != loop_thread for ident in decoded_on)
    assert not instance.fragments.buffers


@pytest.mark.asyncio
async def test_small_frames_never_submit_work(monkeypatch):
    values = ["ping", [b"x" * 64] * 256]
    payloads = [(pickle.dumps(values[0]), Flags.RPC)]
    payloads.append((BaseProtocol.batch([pickle.dumps(item) for item in values[1]]), Flags.BATCH))
    instance = receiver(payloads)

    async def unexpected(*args):
        pytest.fail("Small packets must stay on the loop")

    monkeypatch.setattr(instance, "decode_in_thread", unexpected)
    assert (await instance.receive()).payload == values[0]
    assert (await instance.receive()).payload == values[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False, True], ids=["raw", "zlib"])
async def test_one_large_frame_needs_only_one_worker_handoff(monkeypatch, compressed):
    value = b"x" * (BaseProtocol.DECODE_INLINE + 1)
    instance = receiver([(pickle.dumps(value), Flags.RPC)], compressed=compressed)
    submitted = 0
    original = instance.decode_in_thread

    async def observed(function, *args):
        nonlocal submitted
        submitted += 1
        return await original(function, *args)

    monkeypatch.setattr(instance, "decode_in_thread", observed)
    assert (await instance.receive()).payload == value
    assert submitted == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [BaseProtocol.DECODE_INLINE + 1, BaseProtocol.FRAGMENT_SIZE * 3])
async def test_large_module_envelope_stays_on_the_loop(size):
    class Loop:
        def __reduce__(self):
            return asyncio.get_running_loop, ()

    # A module initializer or reconstruction hook can require a running loop.
    # The source envelope must not change that first-load contract.
    body = pickle.dumps([Loop(), b"x" * size])
    instance = receiver([(pickle.dumps(({}, body)), Flags.MODULES)])
    result = (await instance.receive()).payload
    assert result == [asyncio.get_running_loop(), b"x" * size]


@pytest.mark.asyncio
async def test_a_worker_can_import_a_previously_registered_module():
    name = "receive_worker_example"
    source = ModuleSource(
        source="import threading\nloaded_on = threading.get_ident()\nclass Value:\n    pass\n",
        file=f"<{name}>",
        package=False,
    )
    module = ModuleLoader.load(name, {name: source})
    loader = ModuleLoader.install({name: source})
    try:
        value = module.Value()
        value.body = b"x" * (BaseProtocol.DECODE_INLINE + 1)
        payload = pickle.dumps(value)
        # A preceding MODULES envelope can register sources whose classes are
        # first used by a later packet without its own envelope.
        del sys.modules[name]
        instance = receiver([(payload, Flags.RPC)])
        restored = (await instance.receive()).payload
        assert restored.body == value.body
        assert sys.modules[name].loaded_on != threading.get_ident()
    finally:
        sys.modules.pop(name, None)
        with ModuleLoader.lock:
            loader.sources.pop(name, None)


@pytest.mark.asyncio
async def test_cancellation_waits_for_the_dictionary_before_releasing_read_lock(monkeypatch):
    payload = pickle.dumps(b"x" * (BaseProtocol.FRAGMENT_SIZE + 100))
    instance = receiver([(payload, Flags.RPC), (pickle.dumps("next"), Flags.RPC)])
    assert instance.inflate is not None
    decode = instance.decode_frames
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    released = threading.Event()
    finished: list[int] = []
    loop_thread = threading.get_ident()

    def blocked(frames):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10), "Worker was not released"
        result = decode(frames)
        finished.append(threading.get_ident())
        return result

    monkeypatch.setattr(instance, "decode_frames", blocked)
    first = asyncio.create_task(instance.receive())
    second = None
    try:
        await entered.wait()
        assert instance.read_lock.locked()
        first.cancel()
        # A callback queued after cancel observes the task after it handles it.
        handled = asyncio.Event()
        loop.call_soon(handled.set)
        await handled.wait()
        assert not first.done()
        assert instance.read_lock.locked()
        first.cancel()
        second = asyncio.create_task(instance.receive())
        observed = asyncio.Event()
        loop.call_soon(observed.set)
        await observed.wait()
        assert not second.done()
        assert not finished
    finally:
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            assert (await second).payload == "next"
    assert finished[0] != loop_thread
    assert not instance.read_lock.locked()


@pytest.mark.asyncio
async def test_worker_errors_propagate_and_release_the_read_lock():
    instance = receiver([(b"broken pickle" + b"x" * BaseProtocol.DECODE_INLINE, Flags.RPC)])
    with pytest.raises(pickle.UnpicklingError):
        await instance.receive()
    assert not instance.read_lock.locked()


@pytest.mark.asyncio
async def test_cancelling_a_partial_group_keeps_its_dictionary_and_fragments():
    waiting = asyncio.Event()

    class Reader(asyncio.StreamReader):
        reads = 0

        async def readexactly(self, size: int) -> bytes:
            self.reads += 1
            if self.reads == 3:
                waiting.set()
            return await super().readexactly(size)

    reader = Reader()
    instance = BaseProtocol(reader, cast(Any, None))
    instance.inflate = FrameDecompressor(instance.FRAGMENT_SIZE)
    encoder = FrameCompressor(3, 256, 128)
    value = b"partial" * 10000
    payload = pickle.dumps(value)

    def feed(body: bytes, flags: Flags, packet_id: int) -> None:
        wire, packed = encoder.encode(body)
        reader.feed_data(
            instance.PACKET_HEADER.pack(
                instance.MAGIC, flags | (Flags.COMPRESSED if packed else 0), len(wire), packet_id
            )
            + wire
        )

    feed(payload[: instance.FRAGMENT_SIZE], Flags.FRAGMENT | Flags.RPC, 1)
    task = asyncio.create_task(instance.receive())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instance.fragments.total_bytes == instance.FRAGMENT_SIZE
    feed(pickle.dumps("other"), Flags.RPC, 2)
    feed(payload[instance.FRAGMENT_SIZE :], Flags.RPC, 1)
    reader.feed_eof()
    assert (await instance.receive()).payload == "other"
    assert (await instance.receive()).payload == value
    assert not instance.fragments.buffers


@pytest.mark.asyncio
async def test_grouped_fragments_return_an_interleaved_response_first(monkeypatch):
    reader = asyncio.StreamReader()
    instance = BaseProtocol(reader, cast(Any, None))
    instance.inflate = FrameDecompressor(instance.FRAGMENT_SIZE)
    encoder = FrameCompressor(3, 256, 128)
    value = b"x" * instance.FRAGMENT_SIZE * (instance.DECODE_FRAMES + 1)
    payload = pickle.dumps(value)

    def frame(chunk: bytes, flags: Flags, packet_id: int) -> None:
        body, packed = encoder.encode(chunk)
        reader.feed_data(
            instance.PACKET_HEADER.pack(
                instance.MAGIC, flags | (Flags.COMPRESSED if packed else 0), len(body), packet_id
            )
            + body
        )

    frame(payload[: instance.FRAGMENT_SIZE], Flags.FRAGMENT | Flags.RPC, 1)
    frame(pickle.dumps("interleaved"), Flags.RPC, 2)
    for offset in range(instance.FRAGMENT_SIZE, len(payload), instance.FRAGMENT_SIZE):
        flags = Flags.RPC
        if offset + instance.FRAGMENT_SIZE < len(payload):
            flags |= Flags.FRAGMENT
        frame(payload[offset : offset + instance.FRAGMENT_SIZE], flags, 1)
    reader.feed_eof()
    groups: list[int] = []
    decode = instance.decode_frames

    def observed(frames):
        groups.append(len(frames))
        return decode(frames)

    monkeypatch.setattr(instance, "decode_frames", observed)
    assert (await instance.receive()).payload == "interleaved"
    assert (await instance.receive()).payload == value
    assert groups == [2, instance.DECODE_FRAMES, 1]
    assert not instance.fragments.buffers
