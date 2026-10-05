"""Measure what one remote log record costs per RPC call.

Run from the repository root:
    python -m benchmarks.logs --threads 8 32 --calls 100 --rounds 9

Four arms share one process and one connection, and the rounds alternate
between them, because the load of the machine changes between runs:

    no records           the same call without a log record
    with response        the record travels inside the response of its call
    separate packet      every batch of records takes a packet of its own
    filtered             the local logger does not accept the record

The third arm is how the handler worked before the records could travel with a
response. The fourth shows what a record costs when no local handler wants it. CPU of the loop thread is sampled from a coroutine on its own loop,
so it holds the work of the protocol and not the work of the callers. The last
column counts the packets that arrive with Flags.LOG alone.

Compare the rows of one run, not runs made at different times.
"""

import argparse
import asyncio
import json
import logging
import statistics
import sys
import threading
import time
from typing import Any

from benchmarks.log_switch import LogSwitch
from rmote.protocol import Flags
from rmote.sync import Connection
from tests.support.tool_cases.log_spam import LogSpam

ARMS = ("no records", "with response", "separate packet", "filtered")


async def loop_cpu() -> float:
    return time.thread_time()


def silence() -> logging.Logger:
    """Keep the delivered records out of the terminal.

    The local handler keeps nothing and writes nothing, so the measurement
    sees the transport and not the console. The logger is given back, because
    one arm raises its level to have the records filtered.
    """
    logger = logging.getLogger("rmote.remote.delivery-test")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    return logger


class Counter:
    """Count the packets that carry records and nothing else, per arm."""

    def __init__(self, connection: Connection) -> None:
        self.counts: dict[str, int] = dict.fromkeys(ARMS, 0)
        self.arm = ARMS[0]
        protocol = connection._protocol
        assert protocol is not None
        original = protocol.receive

        async def receive() -> Any:
            packet = await original()
            if packet.flags & Flags.LOG and not packet.flags & Flags.RESPONSE:
                self.counts[self.arm] += 1
            return packet

        protocol.receive = receive  # type: ignore[method-assign]


def round_of(connection: Connection, threads: int, calls: int, records: int) -> dict[str, float]:
    """Run *calls* in every thread, each call emitting *records* records."""
    barrier = threading.Barrier(threads)
    spent: list[float] = []
    lock = threading.Lock()
    loop = connection._protocol.loop  # type: ignore[union-attr]

    def worker() -> None:
        barrier.wait()
        start = time.perf_counter()
        for _ in range(calls):
            connection(LogSpam.speak, records)
        with lock:
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
        "loop_cpu_us": (after - before) / total * 1e6,
        "calls_per_second": total / max(spent),
    }


def best(rounds: list[dict[str, float]]) -> dict[str, float]:
    """Take the median of every column, so one slow round cannot decide."""
    return {name: statistics.median(item[name] for item in rounds) for name in rounds[0]}


def measure(
    connection: Connection, counter: Counter, logger: logging.Logger, threads: int, calls: int, rounds: int
) -> dict[str, Any]:
    """Run every arm once per round, so drift of the machine hits them all."""
    collected: dict[str, list[dict[str, float]]] = {arm: [] for arm in ARMS}
    arms = (
        (ARMS[0], 0, True, logging.WARNING),
        (ARMS[1], 1, True, logging.WARNING),
        (ARMS[2], 1, False, logging.WARNING),
        (ARMS[3], 1, True, logging.CRITICAL),
    )
    for index in range(rounds + 1):
        for arm, records, attach, level in arms:
            connection(LogSwitch.attach, attach)
            logger.setLevel(level)
            counter.arm = arm
            values = round_of(connection, threads, calls, records)
            if index:  # the first round only warms the path up
                collected[arm].append(values)
    logger.setLevel(logging.WARNING)
    return {arm: best(values) for arm, values in collected.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, nargs="+", default=[8, 32])
    parser.add_argument("--calls", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--json", action="store_true", help="print the raw numbers as JSON")
    options = parser.parse_args()

    logger = silence()
    results: dict[int, dict[str, Any]] = {}
    counts: dict[int, dict[str, int]] = {}
    with Connection.from_local(python=sys.executable) as connection:
        connection(LogSpam.speak, 0)  # warm up the tool transfer
        counter = Counter(connection)
        for threads in options.threads:
            counter.counts = dict.fromkeys(ARMS, 0)
            results[threads] = measure(connection, counter, logger, threads, options.calls, options.rounds)
            counts[threads] = dict(counter.counts)

    if options.json:
        print(json.dumps({"rounds": results, "log_packets": counts}, indent=2))
        return
    header = f"{'threads':>8}{'log delivery':>20}{'calls/s':>11}{'ratio':>7}{'loop CPU µs':>14}{'µs/record':>14}"
    print(f"{header}{'LOG packets':>13}")
    for threads in options.threads:
        base = results[threads][ARMS[0]]
        for arm in ARMS:
            values = results[threads][arm]
            share = values["calls_per_second"] / base["calls_per_second"]
            per_record = values["loop_cpu_us"] - base["loop_cpu_us"]
            packets = "" if arm == ARMS[0] else str(counts[threads][arm])
            print(
                f"{threads:>8}{arm:>20}{values['calls_per_second']:>11.0f}{share:>7.2f}"
                f"{values['loop_cpu_us']:>14.2f}{per_record:>14.2f}{packets:>13}"
            )


if __name__ == "__main__":
    main()
