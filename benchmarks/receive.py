"""Measure receive throughput and event-loop pauses with journald-shaped data.

Run the same command before and after a receiver change::

    python -m benchmarks.receive --rate 30000 --seconds 5
    python -m benchmarks.receive --rate 30000 --burst 38 --seconds 5
    python -m benchmarks.receive --rate 0 --seconds 5

Frames are prepared before timing, so sender serialization and compression do
not obscure the receiver's cost. The replay has one zlib dictionary, interleaved
stream IDs, and bounded bursts. Rate zero replays as fast as receive can read.
The timer measures scheduling lateness, not the duration of a pickle call.
Use benchmarks.streams separately for end-to-end throughput and small items.
"""

import argparse
import asyncio
import pickle
import statistics
import time
from typing import Any, cast

from rmote.protocol import BaseProtocol, Flags, FrameCompressor, FrameDecompressor


def frames(count: int, batch: int, streams: int) -> list[bytes]:
    codec = FrameCompressor(3, 256, 128)
    result: list[bytes] = []
    for offset in range(0, count, batch):
        items = []
        for index in range(offset, min(offset + batch, count)):
            record = {
                "__CURSOR": f"s=0123456789abcdef;i={index:x};b=fedcba9876543210;m={index * 123456:x}",
                "__REALTIME_TIMESTAMP": str(1792360000000000 + index * 33),
                "__MONOTONIC_TIMESTAMP": str(index * 33),
                "_HOSTNAME": f"host-{index % streams}",
                "_MACHINE_ID": "fedcba98765432100123456789abcdef",
                "_BOOT_ID": "0123456789abcdeffedcba9876543210",
                "_SYSTEMD_UNIT": "example.service",
                "_TRANSPORT": "journal",
                "_PID": str(1000 + index % 300),
                "_UID": "1000",
                "_GID": "1000",
                "PRIORITY": "6",
                "SYSLOG_IDENTIFIER": "example",
                "CODE_FILE": "example/service.py",
                "CODE_LINE": str(index % 500),
                "MESSAGE": f"Request {index}: " + "a representative journal message with repeated words; " * 8,
            }
            items.append(pickle.dumps(record))
        payload = BaseProtocol.batch(items)
        wire = []
        for start in range(0, len(payload), BaseProtocol.FRAGMENT_SIZE):
            body, packed = codec.encode(payload[start : start + BaseProtocol.FRAGMENT_SIZE])
            flags = Flags.RPC | Flags.STREAM | Flags.RESPONSE | Flags.BATCH
            if packed:
                flags |= Flags.COMPRESSED
            if start + BaseProtocol.FRAGMENT_SIZE < len(payload):
                flags |= Flags.FRAGMENT
            wire.append(
                BaseProtocol.PACKET_HEADER.pack(BaseProtocol.MAGIC, flags, len(body), offset // batch % streams)
            )
            wire.append(body)
        result.append(b"".join(wire))
    return result


async def measure(
    wire: list[bytes],
    count: int,
    batch: int,
    rate: int,
    burst: int,
    decode_threshold: int = BaseProtocol.DECODE_INLINE,
) -> dict[str, float]:
    reader = asyncio.StreamReader()
    remote = BaseProtocol(reader, cast(Any, None), decode_threshold=decode_threshold)
    remote.inflate = FrameDecompressor(remote.FRAGMENT_SIZE)
    pauses: list[float] = []
    loop = asyncio.get_running_loop()
    running = True

    async def timer() -> None:
        while running:
            due = loop.time() + 0.001
            await asyncio.sleep(0.001)
            pauses.append(max(0.0, loop.time() - due))

    async def feed() -> None:
        start = loop.time()
        for index, data in enumerate(wire):
            if rate and index % burst == 0:
                await asyncio.sleep(max(0.0, start + index * batch / rate - loop.time()))
            elif not rate:
                # At most one burst is buffered: do not benchmark an unbounded
                # StreamReader that bypasses normal transport backpressure.
                await ready.wait()
                ready.clear()
            reader.feed_data(data)
        reader.feed_eof()

    ready = asyncio.Event()
    ready.set()
    ticker = asyncio.create_task(timer())
    producer = asyncio.create_task(feed())
    start = time.perf_counter()
    received = 0
    try:
        for _ in wire:
            packet = await remote.receive()
            received += len(packet.payload)
            ready.set()
        elapsed = time.perf_counter() - start
        assert received == count
    finally:
        running = False
        await ticker
        await producer
    ordered = sorted(pauses)
    return {
        "items_s": count / elapsed,
        "max_ms": max(pauses) * 1000,
        "p99_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] * 1000,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=30000)
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--streams", type=int, default=38)
    parser.add_argument("--burst", type=int, default=1, help="Batches released together at the same average rate.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--decode-threshold", type=int, default=BaseProtocol.DECODE_INLINE)
    options = parser.parse_args()
    BaseProtocol.check_decode_threshold(options.decode_threshold)
    if min(options.seconds, options.batch, options.streams, options.rounds, options.burst) < 1 or options.rate < 0:
        parser.error("sizes and counts must be positive; rate must be nonnegative")
    count = 30000 * options.seconds
    wire = frames(count, options.batch, options.streams)
    print(
        f"{count} records; batch={options.batch}; streams={options.streams}; rate={options.rate}; burst={options.burst}"
    )
    await measure(
        wire[:4], min(count, options.batch * 4), options.batch, options.rate, options.burst, options.decode_threshold
    )
    rows = [
        await measure(wire, count, options.batch, options.rate, options.burst, options.decode_threshold)
        for _ in range(options.rounds)
    ]
    for index, row in enumerate(rows):
        print(f"round {index + 1}: {row}")
    print("median:", {key: statistics.median(row[key] for row in rows) for key in rows[0]})


if __name__ == "__main__":
    asyncio.run(main())
