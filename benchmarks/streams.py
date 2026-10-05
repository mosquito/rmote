"""Measure what a streaming call costs per item as more streams share a connection.

Run from the repository root::

    python -m benchmarks.streams
    python -m benchmarks.streams --sizes 16 64 256 --streams 1 4 8 16
    python -m benchmarks.streams --connections 2

Every row moves the same number of bytes, so the per-item cost is comparable
across rows. The reads column counts os.read calls of this process per item:
those reads coalesce only while the sender runs ahead of the consumer, and the
cost per item doubles once they stop. CPU is taken from the process itself and,
through a tool, from the interpreter on the other side. Absolute numbers depend
on the load of the machine; compare the rows of one run.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

from benchmarks.rusage import Rusage
from benchmarks.stream_items import Items
from rmote.protocol import Protocol

VOLUME = 16384 * 64


async def measure(remotes: list[Protocol], streams: int, size: int) -> dict[str, float]:
    """Run *streams* streams of *size* items, spread over *remotes*."""
    each = max(1, VOLUME // size // streams)
    original = os.read
    reads: list[int] = []
    finished: list[float] = []

    def counted(fd: int, length: int) -> bytes:
        data = original(fd, length)
        reads.append(len(data))
        return data

    async def drain(remote: Protocol) -> None:
        async for _ in remote.stream(Items.produce, each, size):
            pass
        finished.append(time.perf_counter())

    before = [await remote(Rusage.snapshot) for remote in remotes]
    local = os.times()
    os.read = counted
    start = time.perf_counter()
    await asyncio.gather(*(drain(remotes[index % len(remotes)]) for index in range(streams)))
    spent = time.perf_counter() - start
    os.read = original
    after_local = os.times()
    after = [await remote(Rusage.snapshot) for remote in remotes]
    items = each * streams
    remote_cpu = sum(end[0] + end[1] - begin[0] - begin[1] for begin, end in zip(before, after, strict=True))
    return {
        "wall_us": spent / items * 1e6,
        "reads": len(reads) / items,
        "local_us": (after_local.user + after_local.system - local.user - local.system) / items * 1e6,
        "remote_us": remote_cpu / items * 1e6,
        "spread": (max(finished) - min(finished)) / spent * 100 if streams > 1 else 0.0,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--streams", type=int, nargs="+", default=[1, 2, 4, 8, 16, 32])
    parser.add_argument("--sizes", type=int, nargs="+", default=[64])
    parser.add_argument("--connections", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=3)
    options = parser.parse_args()

    async with AsyncExitStack() as stack:
        remotes = [
            await stack.enter_async_context(await Protocol.from_command(python=sys.executable))
            for _ in range(options.connections)
        ]
        for remote in remotes:
            async for _ in remote.stream(Items.produce, 64, 64):
                pass
            await remote(Rusage.snapshot)
        print(f"{options.connections} connection(s); every row moves {VOLUME // 1024} KiB")
        print(f"{'size':>6}{'streams':>9}{'µs/item':>10}{'reads':>8}{'local µs':>10}{'remote µs':>11}{'spread %':>10}")
        for size in options.sizes:
            for streams in options.streams:
                await measure(remotes, streams, size)
                rounds = [await measure(remotes, streams, size) for _ in range(options.rounds)]
                best: dict[str, Any] = min(rounds, key=lambda row: row["wall_us"])
                median = {name: statistics.median(row[name] for row in rounds) for name in best}
                print(
                    f"{size:>6}{streams:>9}{best['wall_us']:>10.2f}{median['reads']:>8.2f}"
                    f"{median['local_us']:>10.2f}{median['remote_us']:>11.2f}{median['spread']:>10.1f}"
                )


if __name__ == "__main__":
    asyncio.run(main())
