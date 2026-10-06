"""Remote log records must not hold up the calls of the connection.

Local handlers run in the worker thread of LogDelivery, so a handler that
takes its time delays nothing but the records behind it. A burst of records
travels in one packet instead of one packet each, and the records of a served
call travel inside its response, which costs no packet at all.
"""

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rmote.protocol import Flags, LogDelivery, LogRecord, Protocol, RemoteLogHandler
from tests.support.tool_cases.log_spam import LogSpam

pytestmark = pytest.mark.timeout(60)


class Counting(Protocol):
    """Count standalone packets carrying records from the logger under test.

    The count starts with the connection, so no packet escapes it. A patched
    method could not do that: the loop already waits inside receive.
    Unrelated logs, including asyncio timing diagnostics, still get delivered.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.log_packets = 0

    async def receive(self):
        packet = await super().receive()
        if (
            packet.flags & Flags.LOG
            and not packet.flags & Flags.RESPONSE
            and any(item[0] == "delivery-test" for item in packet.payload)
        ):
            self.log_packets += 1
        return packet


def record(name: str = "delivery-test", message: str = "hello") -> LogRecord:
    return name, logging.WARNING, "<remote>", 1, message, None


def text(record: LogRecord) -> str:
    """The message of one record as it travels on the wire."""
    return record[4]


class Capture(logging.Handler):
    """Collect records, their threads, and optionally take a long time."""

    def __init__(self, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self.messages: list[str] = []
        self.threads: list[int] = []

    def emit(self, item: logging.LogRecord) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.messages.append(item.getMessage())
        self.threads.append(threading.get_ident())


@pytest.fixture
def capture(request):
    """Attach a handler to the remote logger hierarchy of this test."""
    handler = Capture(getattr(request, "param", 0.0))
    logger = logging.getLogger("rmote.remote.delivery-test")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    yield handler
    logger.removeHandler(handler)


async def wait_for(capture: Capture, count: int, timeout: float = 10.0) -> None:
    """Wait until *count* records reached the local handler."""
    deadline = time.monotonic() + timeout
    while len(capture.messages) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert len(capture.messages) >= count, f"{len(capture.messages)} records arrived of {count}"


def test_a_slow_handler_runs_outside_the_caller_thread(capture):
    delivery = LogDelivery()
    capture.delay = 0.01
    delivery.deliver([record(message=f"item {index}") for index in range(5)])
    delivery.stop()
    assert capture.messages == [f"item {index}" for index in range(5)]
    assert threading.get_ident() not in capture.threads
    assert len(set(capture.threads)) == 1


def test_a_full_queue_counts_and_reports_the_loss(capture, caplog):
    delivery = LogDelivery(limit=2)
    with caplog.at_level(logging.ERROR, logger="rmote.remote"):
        delivery.deliver([record(message=f"item {index}") for index in range(6)])
        assert delivery.lost == 4
        delivery.stop()
    assert capture.messages == ["item 0", "item 1"]
    assert "Dropped 4 remote log records" in caplog.text


def test_a_failing_handler_does_not_stop_delivery(capture, caplog):
    delivery = LogDelivery()
    broken = Capture()
    broken.emit = lambda item: (_ for _ in ()).throw(RuntimeError("handler is broken"))  # type: ignore[method-assign]
    logger = logging.getLogger("rmote.remote.delivery-test")
    logger.addHandler(broken)
    try:
        with caplog.at_level(logging.ERROR, logger="rmote.remote"):
            delivery.deliver([record(message="first"), record(message="second")])
            delivery.stop()
    finally:
        logger.removeHandler(broken)
    assert capture.messages == ["first", "second"]
    assert "Local handler failed" in caplog.text


def test_stopping_an_idle_delivery_does_nothing():
    delivery = LogDelivery()
    delivery.stop()
    assert delivery.worker is None


@pytest.mark.asyncio
async def test_a_burst_above_the_limit_travels_in_one_packet(capture):
    count = RemoteLogHandler.ATTACH_RECORDS + 16
    async with await Counting.from_command(python=sys.executable) as remote:
        assert await remote(LogSpam.speak_on_loop, count) == count
        await wait_for(capture, count)
        # The burst is above ATTACH_RECORDS, so it keeps its own packet. One
        # packet serves it, not one packet per record.
        assert remote.log_packets == 1


@pytest.mark.asyncio
async def test_a_record_of_a_call_travels_with_its_response(capture):
    async with await Counting.from_command(python=sys.executable) as remote:
        for _ in range(20):
            assert await remote(LogSpam.speak_on_loop, 1) == 1
        await wait_for(capture, 20)
        assert remote.log_packets == 0
    assert capture.messages == ["record 0"] * 20


@pytest.mark.asyncio
async def test_records_of_threaded_calls_are_delivered(capture):
    async with await Protocol.from_command(python=sys.executable) as remote:
        for _ in range(20):
            assert await remote(LogSpam.speak, 1) == 1
        await wait_for(capture, 20)
    # A scheduler delay beyond ATTACH_DELAY may give any record its own
    # packet. Either route must deliver every record exactly once.
    assert capture.messages == ["record 0"] * 20


@pytest.mark.asyncio
async def test_records_keep_their_order(protocol, capture):
    assert await protocol(LogSpam.speak, 24) == 24
    await asyncio.to_thread(protocol._logs.stop)
    assert capture.messages == [f"record {index}" for index in range(24)]


@pytest.mark.asyncio
async def test_a_slow_handler_does_not_hold_up_calls(capture, isolated_remote, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    emit = capture.emit

    def blocked(item):
        entered.set()
        release.wait()
        emit(item)

    monkeypatch.setattr(capture, "emit", blocked)
    # Release an incorrectly inline handler too, so a regression fails
    # instead of leaving the event loop stuck inside logging.
    watchdog = threading.Timer(10.0, release.set)
    watchdog.start()
    try:
        assert await isolated_remote(LogSpam.speak, 1) == 1
        assert await asyncio.to_thread(entered.wait, 10.0)
        for _ in range(19):
            assert await isolated_remote(LogSpam.speak, 1) == 1
        assert not release.is_set(), "RPC calls waited for the blocked log handler"
        assert capture.messages == []
    finally:
        release.set()
        watchdog.cancel()
        watchdog.join()
    await asyncio.to_thread(isolated_remote._logs.stop)
    assert capture.messages == ["record 0"] * 20


@pytest.mark.asyncio
async def test_records_sent_before_the_close_are_delivered(capture):
    import sys

    # The handler is slow on purpose: the records are still in the queue when
    # the connection closes, and the close delivers them.
    capture.delay = 0.01
    async with await Protocol.from_command(python=sys.executable) as remote:
        assert await remote(LogSpam.speak, 8) == 8
    assert capture.messages == [f"record {index}" for index in range(8)]


@pytest.mark.asyncio
async def test_one_scheduled_send_serves_a_whole_burst(capture):
    scheduled: list[Any] = []
    sent: list[Any] = []
    loop = asyncio.get_running_loop()
    protocol = Protocol(asyncio.StreamReader(), cast(asyncio.StreamWriter, None))
    handler = RemoteLogHandler(protocol, loop)

    async def send(payload, flags, packet_id):
        sent.append(payload)

    protocol.send = send  # type: ignore[method-assign]
    handler.loop = cast(asyncio.AbstractEventLoop, SimpleNamespace(call_soon_threadsafe=scheduled.append))
    logger = logging.Logger("burst-test")
    logger.addHandler(handler)
    for index in range(5):
        logger.warning("record %s", index)
    # The records wait for one step of the loop, so one send serves them all.
    assert scheduled == [handler.start]
    handler.loop = loop
    handler.start()
    await asyncio.gather(*protocol._tasks)
    assert len(sent) == 1
    assert [text(item) for item in sent[0]] == [f"record {index}" for index in range(5)]


@pytest.mark.asyncio
async def test_a_record_outside_a_call_keeps_its_own_packet(protocol, capture):
    """A timer of the remote loop has no response to travel with."""
    assert await protocol(LogSpam.speak_later, 0.02, "from a timer") == 0.02
    await wait_for(capture, 1)
    assert capture.messages == ["from a timer"]


@pytest.mark.asyncio
async def test_a_record_after_an_attached_batch_still_travels(protocol, capture):
    """A response that carried a batch leaves the handler ready for the next."""
    assert await protocol(LogSpam.speak_on_loop, 1, "attached") == 1
    assert await protocol(LogSpam.speak_later, 0.02, "from a timer") == 0.02
    await wait_for(capture, 2)
    assert capture.messages == ["attached 0", "from a timer"]


@pytest.mark.asyncio
async def test_a_long_call_does_not_hold_its_records(protocol, capture):
    """A record is delivered even while its RPC is waiting for release."""
    call = asyncio.ensure_future(protocol(LogSpam.speak_until_released))
    try:
        await wait_for(capture, 1)
        assert not call.done()
    finally:
        await protocol(LogSpam.release)
        assert await call == 1
    assert capture.messages == ["record 0"]


@pytest.mark.asyncio
async def test_records_keep_their_order_when_the_ways_are_mixed(protocol, capture):
    """A batch that a response refuses still keeps its place in the order."""
    big = RemoteLogHandler.ATTACH_RECORDS + 16
    expected: list[str] = []
    for index in range(4):
        assert await protocol(LogSpam.speak_on_loop, 1, f"small{index}") == 1
        expected.append(f"small{index} 0")
        assert await protocol(LogSpam.speak_on_loop, big, f"big{index}") == big
        expected.extend(f"big{index} {item}" for item in range(big))
    await wait_for(capture, len(expected))
    assert capture.messages == expected


@pytest.mark.asyncio
async def test_a_batch_that_does_not_fit_stays_out_of_the_response():
    """detach refuses a batch that would make the response a fragmented packet."""
    loop = asyncio.get_running_loop()
    protocol = Protocol(asyncio.StreamReader(), cast(asyncio.StreamWriter, None))
    handler = RemoteLogHandler(protocol, loop)
    # Nothing may run on the loop, so the state stays as the emit left it.
    handler.loop = cast(asyncio.AbstractEventLoop, SimpleNamespace(call_soon_threadsafe=lambda callback: None))
    logger = logging.Logger("limit-test")
    logger.addHandler(handler)

    for index in range(handler.ATTACH_RECORDS + 1):
        logger.warning("record %s", index)
    assert handler.detach() == []
    assert len(handler.pending) == handler.ATTACH_RECORDS + 1
    assert handler.sending is False

    handler.pending.clear()
    logger.warning("x" * (handler.ATTACH_BYTES + 1))
    assert handler.detach() == []
    assert len(handler.pending) == 1

    handler.pending.clear()
    logger.warning("small enough")
    batch = handler.detach()
    assert [text(item[1]) for item in batch] == ["small enough"]
    assert handler.pending == []
    # The wire belongs to this batch until the caller reports the result.
    assert handler.sending is True
    assert handler.detach() == []
    handler.sent()
    assert handler.sending is False


@pytest.mark.asyncio
async def test_records_of_a_failed_response_keep_their_place():
    """A response that never reached the wire gives its records back."""
    loop = asyncio.get_running_loop()
    writer = cast(asyncio.StreamWriter, SimpleNamespace(is_closing=lambda: True))
    protocol = Protocol(asyncio.StreamReader(), writer)
    handler = RemoteLogHandler(protocol, loop)
    protocol.log_handler = handler
    handler.loop = cast(asyncio.AbstractEventLoop, SimpleNamespace(call_soon_threadsafe=lambda callback: None))
    logger = logging.Logger("restore-test")
    logger.addHandler(handler)
    logger.warning("first")

    async def failing(packet, flags, packet_id):
        raise RuntimeError("no transport")

    protocol.send = failing  # type: ignore[method-assign]
    await protocol._send_response(42, Flags.RPC | Flags.RESPONSE, 7)
    assert [text(item[1]) for item in handler.pending] == ["first"]
    assert handler.sending is False


@pytest.mark.asyncio
async def test_a_drain_waits_for_the_batch_on_the_wire():
    """Records that came later must not pass a batch that is being written."""
    loop = asyncio.get_running_loop()
    sent: list[Any] = []
    protocol = Protocol(asyncio.StreamReader(), cast(asyncio.StreamWriter, None))
    handler = RemoteLogHandler(protocol, loop)

    async def send(payload, flags, packet_id):
        sent.append(payload)

    protocol.send = send  # type: ignore[method-assign]
    handler.loop = cast(asyncio.AbstractEventLoop, SimpleNamespace(call_soon_threadsafe=lambda callback: None))
    logger = logging.Logger("order-test")
    logger.addHandler(handler)

    logger.warning("first")
    held = handler.detach()
    assert [text(item[1]) for item in held] == ["first"]
    logger.warning("second")
    await handler.drain()
    assert sent == []
    assert [text(item[1]) for item in handler.pending] == ["second"]
    assert handler.draining is False

    handler.loop = loop
    handler.sent()
    await asyncio.gather(*protocol._tasks)
    assert [text(item) for packet in sent for item in packet] == ["second"]


def test_a_record_the_local_logger_refuses_never_reaches_the_worker(capture):
    """The level is tested where the packet arrives, so nothing is handed off."""
    logger = logging.getLogger("rmote.remote.delivery-test")
    logger.setLevel(logging.CRITICAL)
    delivery = LogDelivery()
    try:
        delivery.deliver([record(message="filtered")])
        assert delivery.worker is None
        assert not delivery.records
        logger.setLevel(logging.WARNING)
        delivery.deliver([record(message="wanted")])
        delivery.stop()
    finally:
        logger.setLevel(logging.WARNING)
    assert capture.messages == ["wanted"]


def test_a_stopped_delivery_starts_no_worker(capture):
    """The connection that produced the records is gone, so nothing restarts."""
    delivery = LogDelivery()
    delivery.deliver([record(message="first")])
    delivery.stop()
    delivery.deliver([record(message="second")])
    assert delivery.worker is None
    assert capture.messages == ["first"]


class Late:
    """A deque that receives one record exactly when the worker asks.

    It stands for the race the worker protects itself against: a record that
    arrives after the worker found nothing and before it sleeps.
    """

    def __init__(self, late: LogRecord) -> None:
        self.items: deque[LogRecord] = deque()
        self.late = late
        self.asked = 0

    def popleft(self) -> LogRecord:
        return self.items.popleft()

    def __bool__(self) -> bool:
        self.asked += 1
        if self.asked == 1:
            self.items.append(self.late)
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)


def test_the_worker_looks_again_before_it_sleeps(capture):
    """A record that arrives while the worker decides to sleep is not missed."""
    delivery = LogDelivery()
    slept = []

    class Watch:
        """Stands for the event, and ends the loop instead of blocking."""

        def wait(self, timeout: float | None = None) -> bool:
            slept.append(True)
            delivery.closing = True
            return True

        def set(self) -> None:
            pass

        def clear(self) -> None:
            pass

    delivery.wake = cast(Any, Watch())
    delivery.records = cast(Any, Late(record(message="arrived")))
    delivery.run()

    # The record was found although it arrived at the worst moment. The one
    # sleep is the end of the run, after the record was delivered.
    assert capture.messages == ["arrived"]
    assert slept == [True]


def test_the_logger_of_a_name_is_looked_up_once():
    """A record of a known logger costs no lookup in the logging registry."""
    LogDelivery.target.cache_clear()
    first = LogDelivery.target("cache-test")

    assert LogDelivery.target("cache-test") is first
    assert LogDelivery.target.cache_info().hits == 1
