"""Load on one synchronous connection from many caller threads.

Every test answers the same question: a caller that drives the connection from
a thread pool must get its own results back, and the connection must return to
an idle state afterwards.

Measure throughput separately with benchmarks.sync_cost; timing ratios depend
on the machine's load and are not part of the connection's contract.
"""

import asyncio
import hashlib
import os
import random
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from rmote.protocol import Flags
from rmote.sync import Connection
from tests.support.tool_cases.concurrent_tools import FirstCounter, SecondCounter
from tests.sync.tools import Environment, Load, Methods, Second

pytestmark = pytest.mark.timeout(90)

WORKERS = 16
CALLS = 250
# Three fragments per direction, with FRAGMENT_SIZE at 64 KiB.
BULK = 192 * 1024
# One prime per worker. A power of a prime identifies its own call, because no
# two distinct primes or exponents give the same result.
PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53]
SPAN = 64


def expected_total(start: int) -> int:
    """Add SPAN consecutive integers from *start*, by a closed form."""
    return SPAN * (2 * start + SPAN - 1) // 2


def assert_idle(connection: Connection) -> None:
    """Inspect after completed calls' callbacks, without submitting another RPC."""
    observed: Future[tuple[int, int]] = Future()
    runtime = connection._runtime

    def inspect() -> None:
        assert connection._protocol is not None
        observed.set_result((len(connection._protocol.futures), len(runtime._requests)))

    assert runtime._loop is not None
    runtime._loop.call_soon_threadsafe(inspect)
    assert observed.result(timeout=10) == (0, 0)


def drive(connection: Connection, work, workers: int = WORKERS):
    """Run *work* once per worker, with every worker starting together."""
    start = threading.Barrier(workers)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(work, worker, start) for worker in range(workers)]
        return [future.result(timeout=60) for future in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_many_calls_from_many_threads_keep_results_with_their_callers():
    """Every answer must be the one its own arguments produce.

    The remote host computes, and the caller checks the value against an
    independent calculation. An answer that belongs to another thread has a
    different value, so it cannot pass.
    """

    def work(worker, start):
        start.wait(timeout=10)
        results = []
        for index in range(CALLS):
            if index % 2:
                begin = worker * CALLS + index
                results.append((expected_total(begin), connection(Load.total, begin, SPAN)))
            else:
                results.append((PRIMES[worker] ** (100 + index), connection(Load.power, PRIMES[worker], 100 + index)))
        return results

    with Connection.from_local() as connection:
        batches = drive(connection, work)
        pairs = [pair for batch in batches for pair in batch]
        assert len(pairs) == WORKERS * CALLS
        assert [received for _, received in pairs] == [value for value, _ in pairs]
        # Each call had its own answer, so a swap between callers would fail above.
        assert len({value for value, _ in pairs}) == WORKERS * CALLS
        assert_idle(connection)


def test_concurrent_failures_carry_their_own_token():
    def work(worker, start):
        start.wait(timeout=10)
        messages = []
        for index in range(15):
            token = f"{worker}:{index}"
            if index % 3 == 0:
                with pytest.raises(ValueError) as info:
                    connection(Load.fail, token)
                messages.append(str(info.value))
            else:
                begin = worker * 100 + index
                assert connection(Load.total, begin, SPAN) == expected_total(begin)
        return messages

    with Connection.from_local() as connection:
        batches = drive(connection, work)
        messages = [message for batch in batches for message in batch]
        assert len(messages) == WORKERS * 5
        assert sorted(messages) == sorted(
            f"failed:{worker}:{index}" for worker in range(WORKERS) for index in (0, 3, 6, 9, 12)
        )
        assert_idle(connection)


def test_large_payloads_from_many_threads_interleave_without_damage(monkeypatch):
    workers = 8
    frames: list[tuple[int, bool]] = []

    async def instrument():
        protocol = connection._protocol
        assert protocol is not None
        send_frame = protocol.send_frame

        async def track(payload, flags, packet_id):
            frames.append((packet_id, bool(flags & Flags.FRAGMENT)))
            return await send_frame(payload, flags, packet_id)

        monkeypatch.setattr(protocol, "send_frame", track)

    def work(worker, start):
        request = os.urandom(BULK)
        start.wait(timeout=10)
        token, digest, answer = connection(Load.roundtrip, worker, request, BULK)
        assert token == worker
        assert digest == hashlib.sha256(request).hexdigest()
        assert answer == random.Random(worker).randbytes(BULK)
        return worker

    with Connection.from_local() as connection:
        # Transfer the tool first, so the measured frames carry only payloads.
        assert connection(Load.ping, "warm up") == "warm up"
        connection._runtime.run(instrument)
        assert sorted(drive(connection, work, workers)) == list(range(workers))
        # The requests did not fit one frame, and frames of different packets
        # reached the transport in a mixed order.
        assert sum(1 for _, fragment in frames if fragment) >= workers * 2
        identifiers = [packet_id for packet_id, _ in frames]
        assert len(set(identifiers)) == workers
        assert any(left != right for left, right in zip(identifiers, identifiers[1:], strict=False))
        assert_idle(connection)


def test_small_calls_make_progress_while_large_ones_transfer(monkeypatch):
    """A small RPC completes while a large request is held between fragments."""
    entered = threading.Event()

    async def instrument():
        protocol = connection._protocol
        assert protocol is not None
        send_frame = protocol.send_frame
        release = asyncio.Event()

        async def hold_after_fragment(payload, flags, packet_id):
            await send_frame(payload, flags, packet_id)
            if flags & Flags.FRAGMENT and not entered.is_set():
                entered.set()
                await release.wait()

        monkeypatch.setattr(protocol, "send_frame", hold_after_fragment)
        return release

    with Connection.from_local() as connection:
        assert connection(Load.ping, "warm up") == "warm up"
        release = connection._runtime.run(instrument)
        loop = connection._runtime._loop
        assert loop is not None
        request = os.urandom(BULK)
        with ThreadPoolExecutor(max_workers=2) as pool:
            heavy = pool.submit(connection, Load.roundtrip, 0, request, BULK)
            try:
                assert entered.wait(10), "large request did not send its first fragment"
                assert not heavy.done()
                light = pool.submit(connection, Load.total, 123, SPAN)
                assert light.result(timeout=10) == expected_total(123)
                assert not heavy.done(), "large request must still be waiting for release"
            finally:
                loop.call_soon_threadsafe(release.set)
            token, digest, answer = heavy.result(timeout=10)
        assert token == 0
        assert digest == hashlib.sha256(request).hexdigest()
        assert answer == random.Random(0).randbytes(BULK)
        assert_idle(connection)


def test_first_call_of_every_class_transfers_it_once(monkeypatch):
    calls = {
        Load.ping: "load",
        Second.ping: "second",
        Methods.echo: "methods",
        FirstCounter.increment: 1,
        SecondCounter.increment: 2,
    }
    syncs: list[int] = []

    async def instrument():
        protocol = connection._protocol
        assert protocol is not None
        send = protocol.send_serialized

        async def track(payload, flags, packet_id):
            if flags & Flags.SYNC:
                syncs.append(packet_id)
            return await send(payload, flags, packet_id)

        monkeypatch.setattr(protocol, "send_serialized", track)

    methods = [*calls] * 4

    def work(worker, start):
        method = methods[worker]
        start.wait(timeout=10)
        return method, connection(method, calls[method])

    with Connection.from_local() as connection:
        connection._runtime.run(instrument)
        results = drive(connection, work, len(methods))
        for method, result in results:
            if method in (FirstCounter.increment, SecondCounter.increment):
                assert result[0] == calls[method]
            else:
                assert result == calls[method]
        # One SYNC per class, however many threads asked for it first.
        assert len(syncs) == len(calls)
        # A class that is already present needs no further transfer.
        assert connection(Environment.inspect)[0]
        assert len(syncs) == len(calls) + 1
        assert_idle(connection)


def test_expired_calls_leave_their_neighbours_untouched(deadline, monkeypatch):
    timer = deadline("rmote.sync")

    async def instrument():
        protocol = connection._protocol
        assert protocol is not None
        send = protocol.send

        async def expire_after_send(payload, flags, packet_id):
            await send(payload, flags, packet_id)
            if flags & Flags.RPC and flags & Flags.REQUEST and payload["method"].endswith("Load.pause"):
                timer.expire_current()

        monkeypatch.setattr(protocol, "send", expire_after_send)

    def work(worker, start):
        start.wait(timeout=10)
        if worker % 2:
            with pytest.raises(TimeoutError):
                connection.call_with_timeout(timer.seconds, Load.pause)
            return f"timeout:{worker}"
        starts = [worker * 1000 + index for index in range(20)]
        totals = [connection.call_with_timeout(10.0, Load.total, begin, SPAN) for begin in starts]
        assert totals == [expected_total(begin) for begin in starts]
        return f"done:{worker}"

    with Connection.from_local() as connection:
        connection._runtime.run(instrument)
        results = drive(connection, work)
        assert sorted(results) == sorted(
            f"timeout:{worker}" if worker % 2 else f"done:{worker}" for worker in range(WORKERS)
        )
        # The expired calls released their futures, and the connection still works.
        assert_idle(connection)
        assert connection(Load.ping, "after timeouts") == "after timeouts"
        assert_idle(connection)
