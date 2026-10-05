"""Concurrent callers and background events through a real sync connection."""

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rmote.protocol import Flags, Tool
from rmote.sync import Connection
from tests.support.tool_cases.concurrent_tools import FirstCounter, SecondCounter

pytestmark = pytest.mark.timeout(15)


def test_concurrent_first_call_preserves_results_exceptions_and_state(monkeypatch):
    class Counter(Tool):
        count = 0

        @classmethod
        async def increment(cls, token: int, fail: bool) -> str:
            cls.count += 1
            if fail:
                raise ValueError(f"error:{token}:{cls.count}")
            return f"result:{token}:{cls.count}"

        @classmethod
        def total(cls) -> int:
            return cls.count

    remote = Connection.from_local()
    all_started = threading.Event()
    release_holder = []
    syncs = []
    workers = 12
    start = threading.Barrier(workers + 1)
    pool = ThreadPoolExecutor(max_workers=workers)

    async def instrument():
        protocol = remote._protocol
        assert protocol is not None
        invoke = protocol._call_tool
        send = protocol.send
        release = asyncio.Event()
        release_holder.append(release)
        calls = 0

        async def track_call(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == workers:
                all_started.set()
            return await invoke(*args, **kwargs)

        async def track_send(payload, flags, packet_id):
            if flags & Flags.SYNC:
                syncs.append(packet_id)
                await release.wait()
            return await send(payload, flags, packet_id)

        monkeypatch.setattr(protocol, "_call_tool", track_call)
        monkeypatch.setattr(protocol, "send", track_send)

    async def release_sync():
        release_holder[0].set()

    def call(token):
        start.wait(timeout=5)
        try:
            return remote(Counter.increment, token, token % 2 == 1)
        except ValueError as error:
            return str(error)

    try:
        remote._runtime.run(instrument)
        futures = [pool.submit(call, token) for token in range(workers)]
        start.wait(timeout=5)
        assert all_started.wait(5)
        assert len(syncs) == 1
        remote._runtime.run(release_sync)
        results = [future.result(timeout=5) for future in futures]
        for token, result in enumerate(results):
            prefix = "error" if token % 2 else "result"
            assert result.startswith(f"{prefix}:{token}:")
        assert {int(result.rsplit(":", 1)[1]) for result in results} == set(range(1, workers + 1))
        assert remote(Counter.total) == workers
        assert len(syncs) == 1
    finally:
        remote._runtime.run(release_sync)
        remote.close()
        pool.shutdown(wait=True, cancel_futures=True)


def test_concurrent_classes_from_one_module_share_state():
    remote = Connection.from_local()
    messages = []
    workers = 12
    start = threading.Barrier(workers + 1)
    pool = ThreadPoolExecutor(max_workers=workers)

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logger = logging.getLogger("rmote.remote.rmote-concurrent-module")
    handler = Capture()
    logger.addHandler(handler)

    def call(token):
        start.wait(timeout=5)
        counter = FirstCounter if token % 2 else SecondCounter
        return remote(counter.increment, token)

    try:
        futures = [pool.submit(call, token) for token in range(workers)]
        start.wait(timeout=5)
        results = [future.result(timeout=5) for future in futures]
        assert [result[0] for result in results] == list(range(workers))
        assert {result[1] for result in results} == set(range(1, workers + 1))
        assert len({result[2] for result in results}) == 1
        assert messages == ["concurrent module loaded"]
    finally:
        remote.close()
        pool.shutdown(wait=True, cancel_futures=True)
        logger.removeHandler(handler)


def test_close_finishes_concurrent_rpc_and_concurrent_closers():
    class HeldTool(Tool):
        @staticmethod
        async def wait(token: int) -> None:
            import asyncio
            import logging

            logging.getLogger("rmote-held-rpc").warning("started %s", token)
            await asyncio.Event().wait()

    remote = Connection.from_local(close_timeout=0.1)
    workers = 6
    pool = ThreadPoolExecutor(max_workers=workers + 3)
    all_started = threading.Event()
    started = set()
    close_start = threading.Barrier(4)

    class Capture(logging.Handler):
        def emit(self, record):
            started.add(record.getMessage())
            if len(started) == workers:
                all_started.set()

    logger = logging.getLogger("rmote.remote.rmote-held-rpc")
    handler = Capture()
    logger.addHandler(handler)

    def close():
        close_start.wait(timeout=5)
        remote.close()

    try:
        calls = [pool.submit(remote, HeldTool.wait, token) for token in range(workers)]
        assert all_started.wait(5)
        closers = [pool.submit(close) for _ in range(3)]
        close_start.wait(timeout=5)
        for future in closers:
            future.result(timeout=5)
        for future in calls:
            with pytest.raises(ConnectionError):
                future.result(timeout=5)
        with pytest.raises(RuntimeError, match="closing or closed"):
            remote(HeldTool.wait, 99)
        assert remote._state == "CLOSED"
    finally:
        remote.close()
        pool.shutdown(wait=True, cancel_futures=True)
        logger.removeHandler(handler)


def test_logging_handler_reentry_fails_in_loop_thread_with_debug():
    class LogTool(Tool):
        @staticmethod
        def emit() -> str:
            import logging

            logging.getLogger("rmote-reentry").warning("reentry record")
            return "done"

    remote = Connection.from_local(env={**os.environ, "PYTHONASYNCIODEBUG": "1"})
    handled = threading.Event()
    errors = []
    threads = []

    class Reenter(logging.Handler):
        def emit(self, record):
            threads.append(threading.get_ident())
            for action in (lambda: remote(LogTool.emit), remote.close, remote.__enter__):
                try:
                    action()
                except RuntimeError as error:
                    errors.append(str(error))
            handled.set()

    logger = logging.getLogger("rmote.remote.rmote-reentry")
    handler = Reenter()
    logger.addHandler(handler)
    try:
        assert remote(LogTool.emit) == "done"
        assert handled.wait(5)
        assert len(errors) == 3
        # Handlers run in the delivery thread, and the close waits for it, so a
        # call from there must be refused instead of waiting for itself.
        assert all("log delivery thread" in error for error in errors)
        assert remote._loop_thread is not None
        assert remote._protocol is not None
        worker = remote._protocol._logs.worker
        assert worker is not None
        assert threads == [worker.ident]
        assert threads[0] not in (threading.get_ident(), remote._loop_thread.ident)
    finally:
        logger.removeHandler(handler)
        remote.close()


def test_idle_logs_and_eof_are_processed_without_rpc():
    class IdleTool(Tool):
        @staticmethod
        async def schedule_log() -> None:
            import asyncio
            import logging

            logger = logging.getLogger("rmote-idle-test")
            asyncio.get_running_loop().call_later(0.05, logger.warning, "idle record")

        @staticmethod
        async def schedule_exit() -> None:
            import asyncio
            import os

            asyncio.get_running_loop().call_later(0.05, os._exit, 0)

    remote = Connection.from_local()
    handled = threading.Event()

    class Capture(logging.Handler):
        def emit(self, record):
            if record.getMessage() == "idle record":
                handled.set()

    logger = logging.getLogger("rmote.remote.rmote-idle-test")
    handler = Capture()
    logger.addHandler(handler)

    async def wait_closed():
        assert remote._protocol is not None
        await asyncio.wait_for(remote._protocol.wait_closed(), timeout=5)

    try:
        remote(IdleTool.schedule_log)
        assert handled.wait(5)
        remote(IdleTool.schedule_exit)
        remote._runtime.run(wait_closed)
        with pytest.raises((ConnectionError, EOFError)):
            remote(IdleTool.schedule_log)
    finally:
        logger.removeHandler(handler)
        remote.close()
