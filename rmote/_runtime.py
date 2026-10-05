"""Local event loop for the synchronous client. Never sent to the remote host."""

import asyncio
import math
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, InvalidStateError
from functools import partial
from typing import Any, TypeVar

R = TypeVar("R")


class _Runtime:
    """Own a loop thread and execute coroutine factories on that loop.

    The connection must close its transports and processes before closing the
    runtime. Shutdown waits for cooperative task cancellation and executor work.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "starting"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready: Future[None] = Future()
        self._finished: Future[None] = Future()
        self._requests: set[Future[Any]] = set()
        # Only the loop thread accesses this dictionary.
        self._tasks: dict[Future[Any], asyncio.Task[None]] = {}
        self._thread = threading.Thread(target=self._serve, name="rmote-runtime", daemon=False)
        self._thread.start()
        try:
            self._ready.result()
        except BaseException:
            # This also handles interruption while the loop is starting.
            try:
                self.close()
            except BaseException:
                pass
            raise

    def _check_thread(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("Cannot call runtime from its event loop thread")

    async def _main(self) -> None:
        stop = asyncio.Event()
        with self._lock:
            self._stop = stop
            if self._state == "closing":
                stop.set()
            else:
                self._state = "open"
        self._ready.set_result(None)
        try:
            await stop.wait()
        finally:
            with self._lock:
                self._state = "closing"

    def _serve(self) -> None:
        error: BaseException | None = None
        try:
            with asyncio.Runner() as runner:
                with self._lock:
                    self._loop = runner.get_loop()
                runner.run(self._main())
        except BaseException as exc:
            error = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            with self._lock:
                self._state = "closed"
                requests = tuple(self._requests)
            for future in requests:
                if error is None:
                    future.cancel()
                else:
                    self._set_exception(future, error)
            if error is None:
                self._finished.set_result(None)
            else:
                self._finished.set_exception(error)

    @staticmethod
    def _set_exception(future: Future[Any], error: BaseException) -> None:
        try:
            future.set_exception(error)
        except InvalidStateError:
            # The caller can cancel the future from another thread.
            pass

    async def _execute(self, factory: Callable[[], Coroutine[Any, Any, R]], future: Future[R]) -> None:
        try:
            result = await factory()
        except asyncio.CancelledError:
            future.cancel()
        except BaseException as exc:
            # Contain SystemExit/KeyboardInterrupt as well as ordinary errors.
            # They belong to the caller, not to the runtime's loop thread.
            self._set_exception(future, exc)
        else:
            try:
                future.set_result(result)
            except InvalidStateError:
                pass

    def _start(self, factory: Callable[[], Coroutine[Any, Any, R]], future: Future[R]) -> None:
        if future.cancelled():
            return
        assert self._loop is not None
        coroutine = self._execute(factory, future)
        try:
            task = self._loop.create_task(coroutine)
        except BaseException as exc:
            coroutine.close()
            self._set_exception(future, exc)
            return
        self._tasks[future] = task
        task.add_done_callback(partial(self._task_done, future))

    def _task_done(self, future: Future[Any], task: asyncio.Task[None]) -> None:
        self._tasks.pop(future, None)
        if task.cancelled():
            # Cancellation before _execute starts cannot run its exception handler.
            future.cancel()
        elif (error := task.exception()) is not None:
            self._set_exception(future, error)

    def _cancel(self, future: Future[Any]) -> None:
        task = self._tasks.get(future)
        if task is not None:
            task.cancel()

    async def _await_task(self, future: Future[Any]) -> None:
        """Wait for the work behind *future*, however it ends."""
        task = self._tasks.get(future)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def wait(self, future: Future[Any], *, timeout: float | None = None) -> None:
        """Block until the loop has finished the work behind *future*.

        A caller that stopped waiting still owns the operation it started, so
        it uses this to let the loop finish before it gives up the connection.
        """
        self.run(lambda: self._await_task(future), timeout=timeout)

    def _request_done(self, future: Future[Any]) -> None:
        with self._lock:
            self._requests.discard(future)
            if future.cancelled() and self._state != "closed" and self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._cancel, future)
                except RuntimeError:
                    # Runner may have closed the loop before _serve updates state.
                    if not self._loop.is_closed():
                        raise

    def submit(self, factory: Callable[[], Coroutine[Any, Any, R]]) -> Future[R]:
        """Schedule a coroutine factory without invoking it in the caller's thread."""
        self._check_thread()
        with self._lock:
            if self._state != "open":
                raise RuntimeError("Runtime is closing or closed")
            assert self._loop is not None
            future: Future[R] = Future()
            self._requests.add(future)
            future.add_done_callback(self._request_done)
            try:
                self._loop.call_soon_threadsafe(self._start, factory, future)
            except BaseException:
                self._requests.discard(future)
                raise
            return future

    def run(self, factory: Callable[[], Coroutine[Any, Any, R]], *, timeout: float | None = None) -> R:
        """Wait for a result; timeout or interruption cancels the local operation."""
        self._check_thread()
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be finite and positive, or None")
        future = self.submit(factory)
        try:
            return future.result(timeout)
        except (TimeoutError, KeyboardInterrupt):
            future.cancel()
            raise

    def close(self) -> None:
        """Stop accepting work, finalize the loop's resources, and join its thread."""
        self._check_thread()
        with self._lock:
            if self._state not in ("closing", "closed"):
                self._state = "closing"
                if self._loop is not None and self._stop is not None:
                    self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join()
        self._finished.result()
