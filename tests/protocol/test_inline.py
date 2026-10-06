"""A synchronous Tool method waits in a worker thread unless its author says it cannot.

The hand-off to that thread protects the loop of the remote side from a method
that waits. It also costs more than a method that only returns a value, so the
author can take the protection off one method at a time.
"""

import asyncio
import threading

import pytest

from rmote.protocol import Tool, inline, runs_on_loop
from tests.support.tool_cases.ping_loop import PingForms

pytestmark = pytest.mark.timeout(60)


class Threads(Tool):
    entered = threading.Event()

    @staticmethod
    def in_thread() -> int:
        return threading.get_ident()

    @staticmethod
    @inline
    def on_loop() -> int:
        return threading.get_ident()

    @staticmethod
    @inline
    def loop_ident() -> int:
        import asyncio as remote_asyncio

        # The running loop is only visible from the thread that owns it.
        remote_asyncio.get_running_loop()
        return threading.get_ident()

    @staticmethod
    def waits(path: str) -> str:
        Threads.entered.set()
        with open(path, "rb", buffering=0) as pipe:
            assert pipe.read(1) == b"x"
        return "awake"

    @staticmethod
    async def wait_started() -> None:
        assert await asyncio.to_thread(Threads.entered.wait, 10)

    @staticmethod
    @inline
    def fails() -> None:
        raise ValueError("from the loop")


@pytest.mark.asyncio
async def test_a_marked_method_runs_in_the_thread_of_the_loop(protocol):
    on_loop = await protocol(Threads.loop_ident)
    assert await protocol(Threads.on_loop) == on_loop
    # Without the mark the method answers from a worker thread.
    assert await protocol(Threads.in_thread) != on_loop


@pytest.mark.asyncio
async def test_an_unmarked_method_that_waits_does_not_stop_the_loop(protocol, fifo):
    slow = asyncio.create_task(protocol(Threads.waits, str(fifo.path)))
    try:
        async with asyncio.timeout(10):
            await protocol(Threads.wait_started)
            assert await protocol(PingForms.thread) == 1
            assert await protocol(PingForms.loop) == 1
        assert not slow.done()
    finally:
        fifo.send()
    assert await slow == "awake"


@pytest.mark.asyncio
async def test_a_marked_method_delivers_its_exception(protocol):
    with pytest.raises(ValueError, match="from the loop"):
        await protocol(Threads.fails)
    assert await protocol(PingForms.loop) == 1


@pytest.mark.asyncio
async def test_cancelling_a_marked_call_leaves_the_connection_usable(protocol):
    call = asyncio.create_task(protocol(PingForms.loop))
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await protocol(PingForms.loop) == 1


def test_the_mark_is_visible_in_both_decorator_orders():
    class Order(Tool):
        @staticmethod
        @inline
        def below() -> int:
            return 1

        @inline
        @staticmethod
        def above() -> int:
            return 2

        @staticmethod
        def plain() -> int:
            return 3

    assert runs_on_loop(Order.below)
    assert runs_on_loop(Order.above)
    assert not runs_on_loop(Order.plain)
    assert Order.below() == 1
    assert Order.above() == 2


def test_the_mark_is_refused_for_a_coroutine():
    with pytest.raises(TypeError, match="already runs on the loop"):

        @inline
        async def late() -> int:
            return 1

    with pytest.raises(TypeError, match="already runs on the loop"):

        @inline
        async def stream():
            yield 1


@pytest.mark.asyncio
async def test_a_marked_method_of_an_inline_tool_runs_on_the_loop(isolated_remote):
    class Inline(Tool):
        @staticmethod
        @inline
        def who() -> int:
            import threading as remote_threading

            return remote_threading.get_ident()

        @staticmethod
        def worker() -> int:
            import threading as remote_threading

            return remote_threading.get_ident()

    assert await isolated_remote(Inline.who) != await isolated_remote(Inline.worker)
