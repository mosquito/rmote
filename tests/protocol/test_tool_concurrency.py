"""Tool synchronization, module registration, and executor log forwarding."""

import asyncio
import logging
import sys
import threading

import pytest

from rmote.protocol import Flags, RemoteLogHandler, Tool
from tests.support.tool_cases.idle import Idle

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]


async def test_first_client_sync_is_shared_by_concurrent_callers(client, monkeypatch):
    class Counter(Tool):
        @staticmethod
        def value(number):
            return number

    entered = asyncio.Event()
    release = asyncio.Event()
    syncs = 0

    async def call(payload, flags):
        nonlocal syncs
        if flags & Flags.SYNC:
            syncs += 1
            entered.set()
            await release.wait()
            return None
        return payload["args"][0]

    monkeypatch.setattr(client, "_call", call)
    tasks = [asyncio.create_task(client(Counter.value, number)) for number in range(8)]
    await entered.wait()
    await asyncio.sleep(0)
    assert syncs == 1
    release.set()
    assert await asyncio.gather(*tasks) == list(range(8))
    assert syncs == 1


async def test_cancelled_sync_leader_allows_one_retry(client, monkeypatch):
    class Counter(Tool):
        @staticmethod
        def value():
            return "ok"

    entered = asyncio.Event()
    release = asyncio.Event()
    syncs = 0

    async def call(payload, flags):
        nonlocal syncs
        if flags & Flags.SYNC:
            syncs += 1
            entered.set()
            await release.wait()
            return None
        return "ok"

    monkeypatch.setattr(client, "_call", call)
    leader = asyncio.create_task(client(Counter.value))
    await entered.wait()
    followers = [asyncio.create_task(client(Counter.value)) for _ in range(5)]
    await asyncio.sleep(0)
    followers[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await followers[0]
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    release.set()
    assert await asyncio.gather(*followers[1:]) == ["ok"] * 4
    assert syncs == 2


async def test_module_load_executes_once_and_keeps_shared_state(client, monkeypatch, tmp_path):
    module_name = "rmote_test_concurrent_module"
    marker = tmp_path / "loads"
    source = (
        "from rmote.protocol import Tool\n"
        f"with open({str(marker)!r}, 'a') as marker:\n    marker.write('load\\n')\n"
        "state = []\n"
        "class First(Tool):\n    shared = state\n"
        "class Second(Tool):\n    shared = state\n"
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    builds = 0
    original_to_thread = asyncio.to_thread

    async def build_in_thread(function, *args):
        nonlocal builds
        builds += 1
        if builds == 1:
            entered.set()
            await release.wait()
        return await original_to_thread(function, *args)

    monkeypatch.setattr("rmote.protocol.asyncio.to_thread", build_in_thread)
    first = {
        "name": "First",
        "module": module_name,
        "sources": {module_name: {"source": source, "file": "<probe>", "package": False}},
    }
    second = {
        "name": "Second",
        "module": module_name,
        "sources": {module_name: {"source": source, "file": "<probe>", "package": False}},
    }
    try:
        tasks = [asyncio.create_task(client._load_tool(first, 1))]
        await entered.wait()
        tasks.append(asyncio.create_task(client._load_tool(second, 2)))
        await asyncio.sleep(0)
        assert builds == 1
        release.set()
        await asyncio.gather(*tasks)
        assert marker.read_text() == "load\n"
        first_tool = client.tools[f"{module_name}.First"]
        second_tool = client.tools[f"{module_name}.Second"]
        assert first_tool.shared is second_tool.shared
        first_tool.shared.append("preserved")
        await client._load_tool(first, 3)
        assert client.tools[f"{module_name}.First"] is first_tool
        assert second_tool.shared == ["preserved"]
        assert marker.read_text() == "load\n"
    finally:
        sys.modules.pop(module_name, None)


async def test_duplicate_inline_registration_keeps_instance_and_state(client):
    definition = {"name": "Stateful", "source": "class Stateful(Tool):\n    state = []\n"}
    await asyncio.gather(*(client._load_tool(definition, number) for number in range(8)))
    instance = client.tools["Stateful"]
    instance.state.append("preserved")
    await client._load_tool(definition, 9)
    assert client.tools["Stateful"] is instance
    assert instance.state == ["preserved"]


async def test_load_failure_does_not_poison_retry(client):
    definition = {"name": "Retry", "source": "raise ValueError('load failed')\n"}
    with pytest.raises(ValueError, match="load failed"):
        await client._load_tool(definition, 1)
    assert not client.tools
    definition["source"] = "class Retry(Tool):\n    pass\n"
    await client._load_tool(definition, 2)
    assert "Retry" in client.tools


async def test_executor_logs_are_sent_and_tracked_in_protocol_loop(client, monkeypatch):
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    loop.set_debug(True)
    received = asyncio.Event()
    release = asyncio.Event()
    sender_threads = []
    payloads = []

    async def send(payload, flags, packet_id):
        sender_threads.append(threading.get_ident())
        payloads.append(payload)
        assert flags == Flags.LOG
        assert 0 <= packet_id < 1 << 64
        received.set()
        await release.wait()

    monkeypatch.setattr(client, "send", send)
    logger = logging.Logger("executor-test")
    logger.addHandler(RemoteLogHandler(client, loop))
    try:
        await asyncio.to_thread(logger.warning, "log from executor %s", "ok")
        await received.wait()
        assert sender_threads == [threading.get_ident()]
        # Records travel in batches, so one packet carries a list, and a
        # record is a tuple whose fifth field is the formatted message.
        assert payloads[0][0][4] == "log from executor ok"
        assert len(client._tasks) == 1
        release.set()
        await asyncio.gather(*client._tasks)
        await asyncio.sleep(0)
        assert not client._tasks
        client._closed.set()
        await asyncio.to_thread(logger.warning, "after close")
        await asyncio.sleep(0)
        assert len(payloads) == 1
    finally:
        release.set()
        loop.set_debug(previous_debug)


async def test_log_send_error_does_not_leave_unhandled_task(client, monkeypatch):
    error_handled = asyncio.Event()
    handler = RemoteLogHandler(client, asyncio.get_running_loop())

    async def send(*args):
        raise BrokenPipeError("closed pipe")

    monkeypatch.setattr(client, "send", send)
    monkeypatch.setattr(handler, "handleError", lambda record: error_handled.set())
    logger = logging.Logger("broken-transport")
    logger.addHandler(handler)
    logger.warning("message")
    await error_handled.wait()
    await asyncio.sleep(0)
    assert not client._tasks


async def test_log_ids_fit_existing_unsigned_wire_header(client):
    first, second = client.get_log_id(), client.get_log_id()
    assert first == (1 << 64) - 1
    assert second == first - 1
    for packet_id in (first, second):
        header = client.PACKET_HEADER.pack(client.MAGIC, int(Flags.LOG), 0, packet_id)
        assert client.PACKET_HEADER.unpack(header)[-1] == packet_id


async def test_real_subprocess_forwards_sync_async_and_idle_logs(protocol, fifo, capture_logs):
    class LogTool(Tool):
        @staticmethod
        def emit_sync():
            import logging

            logging.getLogger("rmote-concurrency").info("sync record")
            return "sync"

        @staticmethod
        async def emit_async():
            import logging

            logging.getLogger("rmote-concurrency").warning("async record")
            return "async"

    capture = capture_logs("rmote.remote.rmote-concurrency")
    assert await protocol(LogTool.emit_sync) == "sync"
    assert await protocol(LogTool.emit_async) == "async"
    await protocol(Idle.log, str(fifo.path), "rmote-concurrency")
    fifo.send()
    await asyncio.to_thread(capture.wait, 3)
    assert set(capture.messages) == {"sync record", "async record", "idle record"}
    assert threading.get_ident() not in capture.threads
    assert len(set(capture.threads)) == 1
