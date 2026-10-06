"""Lifecycle and thread-boundary checks for the local synchronous runtime."""

import asyncio
import contextvars
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from types import AsyncGeneratorType

import pytest

from rmote._runtime import _Runtime

pytestmark = pytest.mark.timeout(10)


@pytest.fixture
def runtime() -> Iterator[_Runtime]:
    instance = _Runtime()
    try:
        yield instance
    finally:
        instance.close()


def test_factory_runs_on_ready_loop_and_preserves_context(runtime: _Runtime) -> None:
    caller = threading.get_ident()
    context = contextvars.ContextVar("runtime-test", default="unset")

    def factory():
        loop = asyncio.get_running_loop()
        assert loop.is_running()
        assert threading.get_ident() != caller
        return asyncio.sleep(0, result=(loop, context.get(), threading.current_thread().daemon))

    context.set("first")
    loop, value, daemon = runtime.run(factory)
    assert value == "first"
    assert not daemon
    context.set("second")
    same_loop, value, _ = runtime.run(factory)
    assert same_loop is loop
    assert value == "second"


@pytest.mark.parametrize("error", [ValueError("failure"), RuntimeError("failure"), SystemExit(7), KeyboardInterrupt()])
def test_factory_exceptions_reach_caller_without_stopping_loop(runtime: _Runtime, error: BaseException) -> None:
    async def fail() -> None:
        raise error

    with pytest.raises(type(error)) as raised:
        runtime.run(fail)
    assert raised.value is error
    assert runtime.run(lambda: asyncio.sleep(0, result="still open")) == "still open"


def test_exception_during_factory_creation_reaches_caller(runtime: _Runtime) -> None:
    def factory():
        raise ValueError("cannot create coroutine")

    with pytest.raises(ValueError, match="cannot create coroutine"):
        runtime.run(factory)
    assert runtime.run(lambda: asyncio.sleep(0, result=1)) == 1


def test_concurrent_callers_get_their_own_results(runtime: _Runtime) -> None:
    with ThreadPoolExecutor(max_workers=8) as callers:
        futures = [callers.submit(runtime.run, lambda i=i: asyncio.sleep(0, result=i)) for i in range(40)]
        assert [future.result(timeout=5) for future in futures] == list(range(40))


def test_cancel_before_start_does_not_call_factory(runtime: _Runtime) -> None:
    entered = threading.Event()
    release = threading.Event()
    called = threading.Event()

    async def hold_loop() -> None:
        entered.set()
        assert release.wait(5)

    def factory():
        called.set()
        return asyncio.sleep(0)

    blocking = runtime.submit(hold_loop)
    try:
        assert entered.wait(5)
        future = runtime.submit(factory)
        assert future.cancel()
    finally:
        release.set()
    blocking.result(timeout=5)
    runtime.run(lambda: asyncio.sleep(0))
    assert future.cancelled()
    assert not called.is_set()


def test_cancel_running_request_runs_its_cleanup(runtime: _Runtime) -> None:
    entered = threading.Event()
    cleaned = threading.Event()

    async def wait() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    future = runtime.submit(wait)
    assert entered.wait(5)
    assert future.cancel()
    assert cleaned.wait(5)
    assert runtime.run(lambda: asyncio.sleep(0, result=2)) == 2


def test_wait_returns_after_the_work_of_a_future_ends(runtime: _Runtime) -> None:
    entered = threading.Event()
    cleaned = threading.Event()

    async def wait() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            cleaned.set()

    future = runtime.submit(wait)
    assert entered.wait(5)
    assert future.cancel()
    # A caller that gave up still owns the operation, so wait() returns only
    # after the loop has finished with it.
    runtime.wait(future)
    assert cleaned.is_set()


def test_wait_on_a_finished_future_returns_at_once(runtime: _Runtime) -> None:
    future = runtime.submit(lambda: asyncio.sleep(0, result=1))
    assert future.result(5) == 1
    runtime.wait(future)


def test_timeout_cancels_wait_and_keeps_runtime_open(runtime: _Runtime) -> None:
    cleaned = threading.Event()

    async def wait() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(TimeoutError):
        runtime.run(wait, timeout=0.05)
    assert cleaned.wait(5)
    assert runtime.run(lambda: asyncio.sleep(0, result=3)) == 3


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("-inf"), float("nan")])
def test_invalid_timeout_rejects_factory(runtime: _Runtime, timeout: float) -> None:
    called = threading.Event()

    def factory():
        called.set()
        return asyncio.sleep(0)

    with pytest.raises(ValueError, match="finite and positive"):
        runtime.run(factory, timeout=timeout)
    assert not called.is_set()


def test_keyboard_interrupt_cancels_running_wait_in_subprocess() -> None:
    script = """
import asyncio
import os
import signal
import threading
from rmote._runtime import _Runtime

entered = threading.Event()
cleaned = threading.Event()
runtime = _Runtime()

async def wait():
    entered.set()
    try:
        await asyncio.Event().wait()
    finally:
        cleaned.set()

def interrupt():
    assert entered.wait(5)
    os.kill(os.getpid(), signal.SIGINT)

sender = threading.Thread(target=interrupt)
sender.start()
try:
    try:
        runtime.run(wait)
    except KeyboardInterrupt:
        assert cleaned.wait(5)
    else:
        raise AssertionError('KeyboardInterrupt was not forwarded')
    assert runtime.run(lambda: asyncio.sleep(0, result=4)) == 4
finally:
    sender.join()
    runtime.close()
print('clean interrupt')
"""
    process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=8)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "clean interrupt"
    assert not process.stderr


def test_interrupt_from_outside_the_process_stops_a_main_thread_wait() -> None:
    """A terminal signal must stop the wait, whichever thread receives it.

    The sender is another process, so only the main thread and the threads of
    the runtime can receive the signal, as with a terminal. The wait of the
    main thread must end for every signal, including one that arrives just
    before the wait starts.
    """
    script = """
import asyncio
import os
import subprocess
import sys
import threading
import time
from rmote._runtime import _Runtime

SENDER = "import os, signal, sys, time; time.sleep(0.05); os.kill(int(sys.argv[1]), signal.SIGINT)"
runtime = _Runtime()
try:
    for attempt in range(6):
        entered = threading.Event()

        async def wait():
            entered.set()
            await asyncio.Event().wait()

        future = runtime.submit(wait)
        assert entered.wait(5)
        sender = subprocess.Popen([sys.executable, "-c", SENDER, str(os.getpid())])
        started = time.monotonic()
        try:
            runtime.result(future)
        except KeyboardInterrupt:
            elapsed = time.monotonic() - started
            assert elapsed < 2, f"attempt {attempt}: the interrupt needed {elapsed:.2f}s"
        else:
            raise AssertionError(f"attempt {attempt}: no KeyboardInterrupt")
        finally:
            future.cancel()
            sender.wait()
    assert runtime.run(lambda: asyncio.sleep(0, result=5)) == 5
finally:
    runtime.close()
print("every interrupt arrived")
"""
    process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "every interrupt arrived"


def test_result_respects_a_timeout_in_the_main_thread_and_in_a_worker(runtime: _Runtime) -> None:
    """Both waits of result() end on time: the sliced one and the plain one."""
    future = runtime.submit(lambda: asyncio.Event().wait())
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            runtime.result(future, 0.1)
        assert 0.05 < time.monotonic() - started < 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                pool.submit(runtime.result, future, 0.1).result(5)
            assert 0.05 < time.monotonic() - started < 2
    finally:
        future.cancel()
        runtime.wait(future, timeout=5)


def test_interrupted_request_registration_does_not_block_close() -> None:
    script = """
import asyncio
import sys
import threading
from concurrent.futures import Future
from rmote._runtime import _Runtime

runtime = _Runtime()
called = False

def factory():
    global called
    called = True
    return asyncio.sleep(0)

def interrupt_registration(frame, event, arg):
    # Reproduce SIGINT arriving before Condition.__exit__ releases the
    # future's lock. That future must never reach runtime shutdown.
    if (event == 'call'
            and frame.f_code is threading.Condition.__exit__.__code__
            and frame.f_back.f_code is Future.add_done_callback.__code__):
        raise KeyboardInterrupt
    return interrupt_registration

try:
    sys.settrace(interrupt_registration)
    try:
        runtime.submit(factory)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError('Registration was not interrupted')
    finally:
        sys.settrace(None)
    assert not called
    assert runtime.run(lambda: asyncio.sleep(0, result=4)) == 4
finally:
    runtime.close()
print('closed after interrupted registration')
"""
    process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=8)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "closed after interrupted registration"
    assert not process.stderr


def test_requests_and_gates_leave_nothing_behind(runtime: _Runtime) -> None:
    """Every end of a request must clear both registries of the runtime."""

    async def value() -> int:
        return 7

    async def fail() -> None:
        raise FileNotFoundError("remote file")

    async def forever() -> None:
        await asyncio.Event().wait()

    for _ in range(5):
        assert runtime.run(value) == 7
        with pytest.raises(FileNotFoundError):
            runtime.run(fail)
        with pytest.raises(TimeoutError):
            runtime.run(forever, timeout=0.05)
        future = runtime.submit(forever)
        assert future.cancel()
        runtime.wait(future, timeout=5)

    # The gates are weakly keyed, so a request that never reached its callback
    # could not keep an entry either.
    assert not runtime._requests
    assert not list(runtime._gates)
    assert runtime.run(value) == 7


def test_loop_thread_rejects_submit_run_and_close(runtime: _Runtime) -> None:
    called = threading.Event()

    def factory():
        called.set()
        return asyncio.sleep(0)

    async def check() -> None:
        with pytest.raises(RuntimeError, match="event loop thread"):
            runtime.submit(factory)
        with pytest.raises(RuntimeError, match="event loop thread"):
            runtime.run(factory)
        with pytest.raises(RuntimeError, match="event loop thread"):
            runtime.close()

    runtime.run(check)
    assert not called.is_set()


def test_close_cancels_requests_and_background_tasks(runtime: _Runtime) -> None:
    request_started = threading.Event()
    background_started = threading.Event()
    request_cleaned = threading.Event()
    background_cleaned = threading.Event()

    async def background() -> None:
        background_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            background_cleaned.set()

    async def wait() -> None:
        asyncio.create_task(background())
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cleaned.set()

    future = runtime.submit(wait)
    assert request_started.wait(5)
    assert background_started.wait(5)
    runtime.close()
    assert future.cancelled()
    assert request_cleaned.is_set()
    assert background_cleaned.is_set()
    assert not runtime._thread.is_alive()


def test_close_finalizes_async_generators(runtime: _Runtime) -> None:
    cleaned = threading.Event()

    async def generator() -> AsyncIterator[str]:
        try:
            yield "first"
        finally:
            cleaned.set()

    async def open_generator() -> AsyncIterator[str]:
        value = generator()
        assert await anext(value) == "first"
        return value

    value = runtime.run(open_generator)
    runtime.close()
    assert cleaned.is_set()
    assert isinstance(value, AsyncGeneratorType)
    assert value.ag_frame is None


def test_close_waits_for_executor_work(runtime: _Runtime) -> None:
    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()

    def worker() -> None:
        entered.set()
        assert release.wait(5)
        worker_finished.set()

    future = runtime.submit(lambda: asyncio.to_thread(worker))
    try:
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as closer:
            closing = closer.submit(runtime.close)
            try:
                with pytest.raises(TimeoutError):
                    closing.result(timeout=0.05)
            finally:
                release.set()
            closing.result(timeout=5)
    finally:
        release.set()
    assert worker_finished.is_set()
    assert future.cancelled()
    assert not runtime._thread.is_alive()


def test_repeated_and_concurrent_close_rejects_new_factories(runtime: _Runtime) -> None:
    with ThreadPoolExecutor(max_workers=4) as closers:
        calls = [closers.submit(runtime.close) for _ in range(4)]
        for call in calls:
            call.result(timeout=5)
    runtime.close()
    called = threading.Event()

    def factory():
        called.set()
        return asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="closing or closed"):
        runtime.submit(factory)
    assert not called.is_set()


def test_submit_close_race_always_finishes_accepted_requests(runtime: _Runtime) -> None:
    barrier = threading.Barrier(9)

    def caller(i: int):
        barrier.wait(timeout=5)
        try:
            return runtime.submit(lambda: asyncio.sleep(0, result=i))
        except RuntimeError:
            return None

    def close() -> None:
        barrier.wait(timeout=5)
        runtime.close()

    with ThreadPoolExecutor(max_workers=9) as workers:
        callers = [workers.submit(caller, i) for i in range(8)]
        closing = workers.submit(close)
        futures = [caller.result(timeout=5) for caller in callers]
        closing.result(timeout=5)
    for i, future in enumerate(futures):
        if future is not None:
            try:
                assert future.result(timeout=1) == i
            except CancelledError:
                pass
    assert not runtime._thread.is_alive()


def test_close_during_request_registration_rejects_factory(runtime: _Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    registering = threading.Event()
    release = threading.Event()
    called = threading.Event()
    add_done_callback = Future.add_done_callback

    def register(future, callback):
        add_done_callback(future, callback)
        if callback == runtime._request_done:
            registering.set()
            assert release.wait(5)

    def factory():
        called.set()
        return asyncio.sleep(0)

    monkeypatch.setattr(Future, "add_done_callback", register)
    with ThreadPoolExecutor(max_workers=2) as workers:
        submission = workers.submit(runtime.submit, factory)
        try:
            assert registering.wait(5)
            workers.submit(runtime.close).result(timeout=3)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="closing or closed"):
            submission.result(timeout=5)
    assert not called.is_set()


def test_failed_loop_start_does_not_leave_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = set(threading.enumerate())
    failure = RuntimeError("cannot create loop")

    def fail():
        raise failure

    monkeypatch.setattr(asyncio.events, "new_event_loop", fail)
    with pytest.raises(RuntimeError, match="cannot create loop") as raised:
        # Close an unexpectedly successful construction before reporting failure.
        instance = _Runtime()
        instance.close()
    assert raised.value is failure
    assert set(threading.enumerate()) == threads


def test_interrupted_start_joins_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = set(threading.enumerate())
    result = Future.result
    interrupted = False

    def interrupt_once(self, timeout=None):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result(self, timeout)

    monkeypatch.setattr(Future, "result", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        _Runtime()
    assert set(threading.enumerate()) == threads


def test_failed_initialization_closes_created_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = set(threading.enumerate())
    loops: list[asyncio.AbstractEventLoop] = []
    new_loop = asyncio.events.new_event_loop

    def create_loop() -> asyncio.AbstractEventLoop:
        loop = new_loop()
        loops.append(loop)
        return loop

    def fail():
        raise RuntimeError("cannot initialize runtime")

    monkeypatch.setattr(asyncio.events, "new_event_loop", create_loop)
    monkeypatch.setattr(asyncio, "Event", fail)
    with pytest.raises(RuntimeError, match="cannot initialize runtime"):
        _Runtime()
    assert loops and all(loop.is_closed() for loop in loops)
    assert set(threading.enumerate()) == threads
