"""Measure what the wire layer of the protocol costs: latency, calls, bytes.

Run from the repository root::

    python -m benchmarks.wire
    python -m benchmarks.wire --rounds 21

The first table decomposes the round trip of one call: the work this side does
before the bytes leave, and everything else, which is the two passes through
the pipe plus the whole remote side. The second table shows how the throughput
of one connection grows with the number of calls in flight. The third shows
bytes per second for content that compresses and for content that does not,
because the policy of send_serialized refuses to compress dense data.

Absolute numbers depend on the machine and its load; compare the rows of one
run, and keep a note of what else was running.
"""

import argparse
import asyncio
import os
import sys
import time
from typing import Any

from benchmarks.stream_items import Items
from rmote.protocol import BaseProtocol, Flags, Protocol
from tests.support.tool_cases.ping import Ping

MIB = 1 << 20
SIZES = (8, 100, 1024, 16 << 10, 256 << 10, MIB)


class Timed(Protocol):
    """Protocol that keeps the time its own side spends on every call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.serialized = 0.0
        self.sent = 0.0

    def serialize(self, value: Any, flags: Flags) -> Any:
        start = time.perf_counter()
        try:
            return super().serialize(value, flags)
        finally:
            self.serialized += time.perf_counter() - start

    async def send_serialized(self, payload: bytes, flags: Flags, packet_id: int) -> None:
        start = time.perf_counter()
        try:
            await super().send_serialized(payload, flags, packet_id)
        finally:
            self.sent += time.perf_counter() - start


async def latency(remote: Timed, rounds: int) -> None:
    print(f"{'payload':>10}{'RTT µs':>10}{'serialize':>11}{'send':>8}{'elsewhere':>11}")
    for size in SIZES:
        payload = b"x" * size
        await remote(Items.echo, payload)
        samples = []
        for _ in range(rounds):
            remote.serialized = remote.sent = 0.0
            start = time.perf_counter()
            await remote(Items.echo, payload)
            samples.append((time.perf_counter() - start, remote.serialized, remote.sent))
        best = min(samples, key=lambda row: row[0])
        total, serialized, sent = (value * 1e6 for value in best)
        print(f"{size:>10}{total:>10.1f}{serialized:>11.1f}{sent:>8.1f}{total - serialized - sent:>11.1f}")


async def concurrency(remote: Timed, rounds: int) -> None:
    print(f"\n{'in flight':>10}{'µs/call':>10}{'calls/s':>10}")
    for width in (1, 8, 64):

        async def batch(count: int = width) -> None:
            for _ in range(256 // count):
                await asyncio.gather(*(remote(Ping.ping) for _ in range(count)))

        await batch()
        samples = []
        for _ in range(max(3, rounds // 3)):
            start = time.perf_counter()
            await batch()
            samples.append(time.perf_counter() - start)
        best = min(samples)
        print(f"{width:>10}{best / 256 * 1e6:>10.1f}{256 / best:>10.0f}")


async def bytes_per_second(remote: Timed, rounds: int) -> None:
    print(f"\n{'payload':>10}{'content':>14}{'upload MiB/s':>14}{'download MiB/s':>16}")
    for size in (MIB, 8 * MIB):
        for label, payload in (("compressible", b"rmote " * (size // 6)), ("random", os.urandom(size))):
            payload = payload[:size]
            await remote(Items.sink, payload)
            uploads = []
            downloads = []
            for _ in range(max(3, rounds // 3)):
                start = time.perf_counter()
                await remote(Items.sink, payload)
                uploads.append(time.perf_counter() - start)
                start = time.perf_counter()
                await remote(Items.source, size, label == "random")
                downloads.append(time.perf_counter() - start)
            volume = size / MIB
            print(f"{size // MIB:>9} MiB{label:>14}{volume / min(uploads):>14.1f}{volume / min(downloads):>16.1f}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rounds", type=int, default=9)
    options = parser.parse_args()
    async with await Timed.from_command(python=sys.executable) as remote:
        await remote(Ping.ping)
        print(f"frame {BaseProtocol.FRAGMENT_SIZE // 1024} KiB, compression level {BaseProtocol.COMPRESSION_LEVEL}")
        await latency(remote, options.rounds)
        await concurrency(remote, options.rounds)
        await bytes_per_second(remote, options.rounds)


if __name__ == "__main__":
    asyncio.run(main())
