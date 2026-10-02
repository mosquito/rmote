"""Measure local sync/async RPC and private runtime cost with stdlib only.

Run from the repository root:
    python -m benchmarks.sync_runtime --samples 5 --connections 1 4

RSS uses the POSIX ps command and is reported separately for this process and
remote interpreters. SSH, network latency, and a shared sync runtime are excluded.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from rmote._runtime import _Runtime
from rmote.protocol import Protocol, Tool
from rmote.sync import Connection
from rmote.tools.fs import FileSystem


def rss_kib(pids: list[int]) -> int:
    output = subprocess.check_output(["ps", "-o", "rss=", "-p", ",".join(map(str, pids))], text=True)
    return sum(int(line) for line in output.splitlines() if line.strip())


def snapshot() -> dict[str, Any]:
    return {
        "main_rss_kib": rss_kib([os.getpid()]),
        "threads": dict(Counter(thread.name for thread in threading.enumerate())),
    }


def latency(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": statistics.median(ordered) * 1000,
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000,
    }


def runtime_case(count: int, idle: float) -> dict[str, Any]:
    before = snapshot()
    runtimes: list[_Runtime] = []
    start = time.perf_counter()
    try:
        for _ in range(count):
            runtimes.append(_Runtime())
        startup = time.perf_counter() - start
        opened = snapshot()
        cpu, wall = time.process_time(), time.perf_counter()
        time.sleep(idle)
        idle_cpu = (time.process_time() - cpu) / (time.perf_counter() - wall) * 100
    finally:
        start = time.perf_counter()
        for runtime in reversed(runtimes):
            runtime.close()
        shutdown = time.perf_counter() - start
    after = snapshot()
    assert after["threads"] == before["threads"], "Runtime threads leaked"
    return {
        "mode": "runtime_only",
        "connections": count,
        "startup_ms": startup * 1000,
        "shutdown_ms": shutdown * 1000,
        "idle_main_cpu_percent": idle_cpu,
        "before": before,
        "opened": opened,
        "after": after,
    }


def sync_case(count: int, iterations: int, concurrency: int, idle: float) -> dict[str, Any]:
    before = snapshot()
    connections: list[Connection] = []
    processes: list[asyncio.subprocess.Process] = []
    start = time.perf_counter()
    try:
        for _ in range(count):
            connection = Connection.from_local(rpc_timeout=10.0)
            connections.append(connection)
            assert connection._process is not None
            processes.append(connection._process)
        startup = time.perf_counter() - start

        def call(i: int) -> None:
            assert connections[i % count](FileSystem.read_bytes, "/dev/null") == b""

        for i in range(count * 5):
            call(i)
        opened = snapshot()
        remote_rss = rss_kib([process.pid for process in processes])
        cpu, wall = time.process_time(), time.perf_counter()
        time.sleep(idle)
        idle_cpu = (time.process_time() - cpu) / (time.perf_counter() - wall) * 100
        times = []
        for i in range(iterations):
            start = time.perf_counter()
            call(i)
            times.append(time.perf_counter() - start)
        with ThreadPoolExecutor(max_workers=concurrency) as callers:
            list(callers.map(call, range(concurrency)))
            start = time.perf_counter()
            list(callers.map(call, range(iterations)))
            throughput = iterations / (time.perf_counter() - start)
    finally:
        start = time.perf_counter()
        for connection in reversed(connections):
            connection.close()
        shutdown = time.perf_counter() - start
    assert all(process.returncode is not None for process in processes), "Remote process leaked"
    after = snapshot()
    assert after["threads"] == before["threads"], "Connection threads leaked"
    return {
        "mode": "sync",
        "connections": count,
        "startup_ms": startup * 1000,
        "shutdown_ms": shutdown * 1000,
        "idle_main_cpu_percent": idle_cpu,
        "remote_rss_kib": remote_rss,
        "latency": latency(times),
        "concurrent_rpcs_per_second": throughput,
        "before": before,
        "opened": opened,
        "after": after,
    }


async def async_case(count: int, iterations: int, concurrency: int, idle: float) -> dict[str, Any]:
    before = snapshot()
    protocols: list[Protocol] = []
    processes: list[asyncio.subprocess.Process] = []
    start = time.perf_counter()
    try:
        for _ in range(count):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-qui",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append(process)
            protocol = await Protocol.from_subprocess(process)
            protocols.append(protocol)
            await protocol.__aenter__()
        startup = time.perf_counter() - start

        async def call(i: int) -> None:
            assert await protocols[i % count](FileSystem.read_bytes, "/dev/null") == b""

        for i in range(count * 5):
            await call(i)
        opened = snapshot()
        remote_rss = rss_kib([process.pid for process in processes])
        cpu, wall = time.process_time(), time.perf_counter()
        await asyncio.sleep(idle)
        idle_cpu = (time.process_time() - cpu) / (time.perf_counter() - wall) * 100
        times = []
        for i in range(iterations):
            start = time.perf_counter()
            await call(i)
            times.append(time.perf_counter() - start)
        limit = asyncio.Semaphore(concurrency)

        async def limited(i: int) -> None:
            async with limit:
                await call(i)

        await asyncio.gather(*(limited(i) for i in range(concurrency)))
        start = time.perf_counter()
        await asyncio.gather(*(limited(i) for i in range(iterations)))
        throughput = iterations / (time.perf_counter() - start)
    finally:
        start = time.perf_counter()
        for protocol in reversed(protocols):
            await protocol.__aexit__(None, None, None)
        for process in reversed(processes):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()
        shutdown = time.perf_counter() - start
    return {
        "mode": "async",
        "connections": count,
        "startup_ms": startup * 1000,
        "shutdown_ms": shutdown * 1000,
        "idle_main_cpu_percent": idle_cpu,
        "remote_rss_kib": remote_rss,
        "latency": latency(times),
        "concurrent_rpcs_per_second": throughput,
        "before": before,
        "opened": opened,
    }


def soak(rounds: int) -> dict[str, Any]:
    async def pending_count(protocol: Protocol) -> int:
        return len(protocol.futures)

    class Probe(Tool):
        @staticmethod
        async def wait(delay: float) -> None:
            import asyncio

            await asyncio.sleep(delay)

        @staticmethod
        async def background_log(token: str) -> None:
            import asyncio
            import logging

            async def later() -> None:
                await asyncio.sleep(0.05)
                logging.getLogger("rmote-bench").info(token)

            asyncio.create_task(later())

        @staticmethod
        async def background_exit() -> None:
            import asyncio
            import os

            async def later() -> None:
                await asyncio.sleep(0.05)
                os._exit(0)

            asyncio.create_task(later())

    received = threading.Event()

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == "idle-log":
                received.set()

    logger = logging.getLogger("rmote.remote.rmote-bench")
    handler = Capture()
    logger.addHandler(handler)
    before = snapshot()
    samples = []
    start = time.perf_counter()
    try:
        for i in range(rounds):
            with Connection.from_local(rpc_timeout=10.0) as connection:
                for _ in range(5):
                    assert connection(FileSystem.read_bytes, "/dev/null") == b""
                    try:
                        connection.call_with_timeout(0.01, Probe.wait, 0.05)
                    except TimeoutError:
                        pass
                    else:
                        raise AssertionError("Expected a local timeout")
                with ThreadPoolExecutor(max_workers=4) as callers:
                    futures = [callers.submit(connection, FileSystem.read_bytes, "/dev/null") for _ in range(20)]
                    assert all(future.result(timeout=5) == b"" for future in futures)
                connection(Probe.background_log, "idle-log")
                assert received.wait(5), "Remote log was not processed while the caller was idle"
                received.clear()
                assert connection._protocol is not None and connection._process is not None
                protocol, process = connection._protocol, connection._process
                assert connection._runtime.run(partial(pending_count, protocol)) == 0, (
                    "Pending RPC leaked after cancellation"
                )
            assert process.returncode is not None, "Remote process leaked after close"
            current = snapshot()
            assert current["threads"] == before["threads"], "Connection threads leaked"
            samples.append({"round": i + 1, **current})
        with Connection.from_local() as connection:
            connection(Probe.background_exit)
            assert connection._protocol is not None
            connection._runtime.run(connection._protocol.wait_closed, timeout=5.0)
            try:
                connection(FileSystem.read_bytes, "/dev/null")
            except (ConnectionError, EOFError):
                pass
            else:
                raise AssertionError("Idle EOF did not close the connection")
    finally:
        logger.removeHandler(handler)
        handler.close()
    after = snapshot()
    assert after["threads"] == before["threads"], "Threads leaked after soak"
    return {
        "rounds": rounds,
        "seconds": time.perf_counter() - start,
        "before": before,
        "after": after,
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--connections", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--soak-rounds", type=int, default=20)
    args = parser.parse_args()
    if min(args.samples, args.iterations, args.concurrency, args.soak_rounds, *args.connections) <= 0:
        parser.error("counts must be positive")
    if not math.isfinite(args.idle_seconds) or args.idle_seconds <= 0:
        parser.error("idle-seconds must be finite and positive")
    logging.getLogger().setLevel(logging.WARNING)
    results = []
    for sample in range(args.samples):
        for count in args.connections:
            for mode in ("runtime_only", "async", "sync"):
                print(f"sample={sample + 1} connections={count} mode={mode}", file=sys.stderr, flush=True)
                if mode == "runtime_only":
                    result = runtime_case(count, args.idle_seconds)
                elif mode == "sync":
                    result = sync_case(count, args.iterations, args.concurrency, args.idle_seconds)
                else:
                    result = asyncio.run(async_case(count, args.iterations, args.concurrency, args.idle_seconds))
                    result["after"] = snapshot()
                    assert result["after"]["threads"] == result["before"]["threads"], "Async threads leaked"
                results.append({"sample": sample + 1, **result})
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
                "parameters": vars(args),
                "rpc": "FileSystem.read_bytes('/dev/null')",
                "notes": [
                    "CPU and main RSS exclude remote interpreters; their RSS is reported separately.",
                    "Async startup/shutdown exclude its parent loop lifecycle; sync includes private runtimes.",
                    "RSS is current resident memory; allocator retention after close is not itself a leak.",
                    "Sequential RPC latency is measured after five warmup calls per connection.",
                    "Concurrent throughput uses up to concurrency calls on round-robin connections.",
                    "No SSH, network, shared-runtime measurement, or performance pass threshold.",
                ],
                "results": results,
                "soak": soak(args.soak_rounds),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
