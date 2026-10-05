"""Compare the local CPU that one RPC costs through the sync and async clients.

Run from the repository root:
    python -m benchmarks.sync_cost --threads 1 8 32 --calls 100 --rounds 9

Both clients call the same tool, which returns at once, so the numbers are the
price of the local machinery and not of remote work. CPU is measured per thread
with time.thread_time: the loop thread is sampled from a coroutine on its own
loop, every caller thread samples itself.

Absolute numbers depend on the load of the machine. Compare the columns of one
run, not runs made at different times.
"""

import argparse
import asyncio
import json
import statistics
import sys
import threading
import time
from typing import Any

from rmote.protocol import Protocol
from rmote.sync import Connection
from tests.support.tool_cases.ping import Ping


async def loop_cpu() -> float:
    return time.thread_time()


def sync_round(connection: Connection, threads: int, calls: int) -> dict[str, float]:
    """Run *calls* in every thread and report the CPU each side spent."""
    barrier = threading.Barrier(threads)
    caller_cpu: list[float] = []
    spent: list[float] = []
    lock = threading.Lock()
    loop = connection._protocol.loop  # type: ignore[union-attr]

    def worker() -> None:
        barrier.wait()
        cpu = time.thread_time()
        start = time.perf_counter()
        for _ in range(calls):
            connection(Ping.ping)
        with lock:
            caller_cpu.append(time.thread_time() - cpu)
            spent.append(time.perf_counter() - start)

    before = asyncio.run_coroutine_threadsafe(loop_cpu(), loop).result()
    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for item in workers:
        item.start()
    for item in workers:
        item.join()
    after = asyncio.run_coroutine_threadsafe(loop_cpu(), loop).result()
    total = threads * calls
    return {
        "wall_us": max(spent) / total * 1e6 * threads,
        "loop_cpu_us": (after - before) / total * 1e6,
        "caller_cpu_us": sum(caller_cpu) / total * 1e6,
        "calls_per_second": total / max(spent),
    }


async def async_round(remote: Protocol, width: int, calls: int) -> dict[str, float]:
    """Run *calls* with *width* calls in flight, all on one loop."""
    cpu = time.thread_time()
    start = time.perf_counter()
    for _ in range(calls):
        await asyncio.gather(*(remote(Ping.ping) for _ in range(width)))
    wall = time.perf_counter() - start
    total = width * calls
    return {
        "wall_us": wall / total * 1e6 * width,
        "loop_cpu_us": (time.thread_time() - cpu) / total * 1e6,
        "caller_cpu_us": 0.0,
        "calls_per_second": total / wall,
    }


def best(rounds: list[dict[str, float]]) -> dict[str, float]:
    """Take the median of every column, so one slow round cannot decide."""
    return {name: statistics.median(item[name] for item in rounds) for name in rounds[0]}


def report(label: str, values: dict[str, float]) -> None:
    local = values["loop_cpu_us"] + values["caller_cpu_us"]
    print(
        f"{label:<22}{values['wall_us']:>10.1f}{values['loop_cpu_us']:>10.2f}"
        f"{values['caller_cpu_us']:>10.2f}{local:>10.2f}{values['calls_per_second']:>11.0f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--calls", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--json", action="store_true", help="print the raw numbers as JSON")
    options = parser.parse_args()

    results: dict[str, dict[str, float]] = {}
    with Connection.from_local(python=sys.executable) as connection:
        connection(Ping.ping)  # warm up the tool transfer
        for threads in options.threads:
            sync_round(connection, threads, options.calls)
            results[f"sync {threads}"] = best(
                [sync_round(connection, threads, options.calls) for _ in range(options.rounds)]
            )

    async def asynchronous() -> None:
        async with await Protocol.from_command(python=sys.executable) as remote:
            await remote(Ping.ping)
            for width in options.threads:
                await async_round(remote, width, options.calls)
                results[f"async {width}"] = best(
                    [await async_round(remote, width, options.calls) for _ in range(options.rounds)]
                )

    asyncio.run(asynchronous())

    if options.json:
        print(json.dumps(results, indent=2))
        return
    print(f"{'path':<22}{'wall µs':>10}{'loop CPU':>10}{'caller CPU':>10}{'total CPU':>10}{'calls/s':>11}")
    for threads in options.threads:
        for kind in ("async", "sync"):
            report(f"{kind}, width {threads}", results[f"{kind} {threads}"])
        ratio: Any = results[f"sync {threads}"]
        local_sync = ratio["loop_cpu_us"] + ratio["caller_cpu_us"]
        local_async = results[f"async {threads}"]["loop_cpu_us"]
        print(f"{'  sync/async ratio':<22}{'':>10}{'':>10}{'':>10}{local_sync / local_async:>10.2f}")


if __name__ == "__main__":
    main()
