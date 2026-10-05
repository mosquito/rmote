"""Measure what a file transfer costs in round trips and in bytes per second.

Run from the repository root:
    python -m benchmarks.file_sync --size 128 --blocks 1 4 8 16

One round writes a file of random bytes, uploads it to an empty destination,
and uploads it again without changing it. Every call through the protocol is
counted, so the number is the round trips that the exchange needs, including
the call that opens the remote session and the one that closes it.

Random bytes do not compress, so the numbers are the cost of the exchange and
not of gzip. The remote side is a local Python subprocess, which has almost no
round-trip time: on a link with a long one, multiply the calls by it.

Absolute numbers depend on the load of the machine and on its page cache.
Compare the rows of one run, not runs made at different times.
"""

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from rmote.protocol import Protocol
from rmote.tools import FileSync


class Counter:
    """Count the calls that one transfer makes through the protocol."""

    def __init__(self, protocol: Protocol) -> None:
        self.protocol = protocol
        self.calls = 0

    async def __call__(self, method: Any, *args: Any) -> Any:
        self.calls += 1
        return await self.protocol(method, *args)


async def transfer(protocol: Protocol, source: Path, target: Path, block_size: int) -> tuple[float, int, int]:
    """Upload *source* once and report the seconds, the calls and the bytes sent."""
    counter = Counter(protocol)
    start = time.perf_counter()
    result = await FileSync.upload(counter, source, target, block_size=block_size)
    return time.perf_counter() - start, counter.calls, result.transferred


async def round_of(protocol: Protocol, directory: Path, size: int, block_size: int) -> dict[str, float]:
    """Transfer a fresh file, then transfer it again unchanged."""
    source, target = directory / "source", directory / "target"
    source.write_bytes(os.urandom(size))
    target.unlink(missing_ok=True)
    first_time, first_calls, sent = await transfer(protocol, source, target, block_size)
    assert sent == size, f"the first transfer sent {sent} of {size} bytes"
    again_time, again_calls, resent = await transfer(protocol, source, target, block_size)
    assert resent == 0, f"the repeat sent {resent} bytes"
    return {
        "first_mib": size / first_time / 1024 / 1024,
        "first_calls": first_calls,
        "again_mib": size / again_time / 1024 / 1024,
        "again_calls": again_calls,
    }


def best(rounds: list[dict[str, float]]) -> dict[str, float]:
    """Take the median of every column, so one slow round cannot decide."""
    return {name: statistics.median(item[name] for item in rounds) for name in rounds[0]}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=128, help="file size in MiB (default: 128)")
    parser.add_argument("--blocks", type=int, nargs="+", default=[1, 4, 8, 16], help="block sizes in MiB")
    parser.add_argument("--rounds", type=int, default=3)
    options = parser.parse_args()

    size = options.size * 1024 * 1024
    results: dict[int, dict[str, float]] = {}
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        async with await Protocol.from_command(python=sys.executable) as protocol:
            for block in options.blocks:
                rounds = [
                    await round_of(protocol, directory, size, block * 1024 * 1024) for _ in range(options.rounds)
                ]
                results[block] = best(rounds)

    print(f"{options.size} MiB file of random bytes, median of {options.rounds} transfers")
    print(f"{'block':>8}{'first':>15}{'calls':>9}{'repeat':>15}{'calls':>9}")
    for block, values in results.items():
        print(
            f"{block:>6} MiB{values['first_mib']:>9.1f} MiB/s{values['first_calls']:>9.0f}"
            f"{values['again_mib']:>9.1f} MiB/s{values['again_calls']:>9.0f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
