"""Test rendezvous; timeouts only bound a broken test's wait."""

import asyncio
import os
import select
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal


class Fifo:
    """A signal pipe shared with a subprocess, without polling its files."""

    def __init__(self, path: Path) -> None:
        self.path = path
        os.mkfifo(path)
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def send(self, data: bytes = b"x") -> None:
        assert os.write(self.fd, data) == len(data)

    def receive(self, size: int = 1) -> bytes:
        assert select.select([self.fd], [], [], 10)[0], f"No signal on {self.path}"
        return os.read(self.fd, size)

    def close(self) -> None:
        os.close(self.fd)


class ObservedEvent(asyncio.Event):
    """Expose when an async operation reaches its blocking wait."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def wait(self) -> Literal[True]:
        self.entered.set()
        return await super().wait()


async def wait_blocked(task: asyncio.Task[Any], entered: asyncio.Event) -> None:
    """An operation must reach its wait, rather than finish ahead of it."""
    notification = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait((task, notification), return_when=asyncio.FIRST_COMPLETED)
        assert entered.is_set(), "Operation finished without reaching its wait"
        assert not task.done(), "Operation did not stay blocked"
    finally:
        notification.cancel()
        await asyncio.gather(notification, return_exceptions=True)


class ObservedLock:
    """Keep the real lock, and signal attempted acquisitions before blocking."""

    def __init__(self, lock) -> None:
        self.lock = lock
        self.changed = threading.Condition()
        self.attempts = 0

    def __enter__(self):
        with self.changed:
            self.attempts += 1
            self.changed.notify_all()
        return self.lock.__enter__()

    def __exit__(self, *args):
        return self.lock.__exit__(*args)

    def wait(self, count: int) -> None:
        with self.changed:
            assert self.changed.wait_for(lambda: self.attempts >= count, 10)


class Deadline:
    """Use asyncio's real cancellation machinery, with an explicit trigger."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.entered = threading.Event()
        self.scopes: dict[asyncio.Task[Any] | None, tuple[asyncio.AbstractEventLoop, asyncio.Timeout]] = {}
        self.lock = threading.Lock()

    def __call__(self, seconds: float | None):
        return self if seconds == self.seconds else asyncio.timeout(seconds)

    async def wait_for(self, awaitable, timeout):
        if timeout != self.seconds:
            return await asyncio.wait_for(awaitable, timeout)
        async with self:
            return await asyncio.wait_for(awaitable, None)

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        scope = asyncio.timeout(None)
        await scope.__aenter__()
        with self.lock:
            self.scopes[asyncio.current_task()] = (loop, scope)
        self.entered.set()
        return scope

    async def __aexit__(self, *args):
        with self.lock:
            _, scope = self.scopes.pop(asyncio.current_task())
        return await scope.__aexit__(*args)

    def expire(self) -> None:
        with self.lock:
            scopes = list(self.scopes.values())
        assert scopes
        for loop, scope in scopes:
            loop.call_soon_threadsafe(scope.reschedule, loop.time() - 1)

    def expire_current(self) -> None:
        with self.lock:
            loop, scope = self.scopes[asyncio.current_task()]
        scope.reschedule(loop.time() - 1)


def wait_until(predicate: Callable[[], object], *, timeout: float = 10) -> None:
    """Wait for an OS-visible state that has no notification interface."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Condition was not reached"
        time.sleep(0.01)


async def async_wait_until(predicate: Callable[[], object], *, timeout: float = 10) -> None:
    """As above, without holding the event loop while the state changes."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
