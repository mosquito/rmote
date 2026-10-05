"""A failed start must name its cause, and the cause is often on stderr.

The peer writes a traceback to the stderr of the transport when it cannot run
our code at all, and the transport itself writes there when it refuses to
connect. Those lines are read by this side: the last ones explain a failed
handshake, and the rest is dropped so that a chatty host cannot fill the pipe.
"""

import asyncio
import sys

import pytest

from rmote.protocol import BaseProtocol, Protocol
from tests.support.tool_cases.ping import Ping

pytestmark = pytest.mark.timeout(60)

# A transport that floods its stderr while it passes the protocol through. The
# flood is bounded, because a writer that outlives the transport would hold its
# stderr pipe open and the wait for the process would never return. It is far
# above the capacity of a pipe, so the lines only pass if this side reads them.
#
# The transport runs the interpreter of this test, because the tool travels to
# it. A python3 of the PATH can be another installation without the packages
# of this project.
FLOOD = 'i=0; while [ $i -lt 20000 ]; do echo "noise $i" >&2; i=$((i+1)); done & exec "$@"'


@pytest.mark.asyncio
async def test_a_transport_that_speaks_on_stderr_explains_the_failure():
    """This is the Store alias of Windows: the transport says why and exits."""
    protocol = await Protocol.from_command("sh", "-c", "echo 'Python was not found' >&2; sleep 0.3")
    with pytest.raises(ConnectionError, match="Its transport said: Python was not found"):
        async with protocol:
            pass


@pytest.mark.asyncio
async def test_transport_exits_before_the_ready_write():
    remote = await Protocol.from_command("sh", "-c", "echo bootstrap-failed; echo transport-failed >&2; exit 3")
    process = remote._owned_process
    assert process is not None
    await process.wait()
    with pytest.raises(ConnectionError) as failure:
        async with remote:
            pass
    assert "It said: bootstrap-failed" in str(failure.value)
    assert "Its transport said: transport-failed" in str(failure.value)
    assert remote._owned_process is None
    assert remote._stderr_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("close_stdout", [False, True])
async def test_failed_ready_write_does_not_wait_for_open_output_pipes(close_stdout):
    script = "import os, time; os.close(0); "
    if close_stdout:
        script += "os.close(1); "
    script += "os.write(2, b'transport-failed\\n'); time.sleep(60)"
    remote = await Protocol.from_command(sys.executable, "-c", script)
    process = remote._owned_process
    assert process is not None
    try:
        async with asyncio.timeout(5):
            while not remote.transport_said:
                await asyncio.sleep(0.001)
        with pytest.raises(ConnectionError, match="Its transport said: transport-failed"):
            async with asyncio.timeout(3), remote:
                pass
        assert remote._owned_process is None
        assert process.returncode is not None
    finally:
        if remote._owned_process is not None:
            await remote.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_an_interpreter_that_cannot_start_explains_itself():
    """A missing module of the standard library is a fatal error on stderr."""
    protocol = await Protocol.from_command(python=sys.executable, env={"PYTHONHOME": "/nonexistent-home"})
    with pytest.raises(ConnectionError, match="No module named 'encodings'"):
        async with protocol:
            pass


@pytest.mark.asyncio
async def test_a_transport_that_floods_its_stderr_keeps_the_calls_working():
    """The pipe is emptied, and only the last lines are kept."""
    async with await Protocol.from_command("sh", "-c", FLOOD, "sh", python=sys.executable) as remote:
        for _ in range(3):
            assert await remote(Ping.ping) == 1
        await asyncio.sleep(0.1)
        assert remote.transport_said
        assert len(remote.transport_said) <= BaseProtocol.MAX_START_FAILURE_LINES


@pytest.mark.asyncio
async def test_a_discarded_stderr_is_not_read():
    """A caller that asks for DEVNULL keeps it, and nothing is captured."""
    async with await Protocol.from_command(python=sys.executable, stderr=asyncio.subprocess.DEVNULL) as remote:
        assert await remote(Ping.ping) == 1
        assert remote._stderr_task is None
        assert not remote.transport_said


@pytest.mark.asyncio
async def test_the_close_does_not_wait_for_the_capture():
    """The task reads a pipe that ends with the transport, so it is cancelled."""
    remote = await Protocol.from_command("sh", "-c", FLOOD, "sh", python=sys.executable)
    async with remote:
        assert await remote(Ping.ping) == 1
        task = remote._stderr_task
        # The flood can end before this line, so the task may already be done.
        # What the close must not do is wait for it.
        assert task is not None
    assert remote._stderr_task is None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_close_releases_paused_pipes_after_the_process_exits():
    remote = await Protocol.from_command(python=sys.executable)
    process = remote._owned_process
    assert process is not None
    transport = process._transport  # type: ignore[attr-defined]
    try:
        async with remote:
            # A full reader pauses its pipe. The process can exit before the
            # reader resumes and sees EOF, so wait() alone cannot release it.
            transport.get_pipe_transport(1).pause_reading()
            transport.get_pipe_transport(2).pause_reading()
            process.terminate()
            async with asyncio.timeout(5):
                while process.returncode is None:
                    await asyncio.sleep(0.001)
        assert transport.is_closing()
        assert all(transport.get_pipe_transport(fd).is_closing() for fd in (0, 1, 2))
    finally:
        transport.close()
        await process.wait()


def test_the_failure_names_both_streams():
    said = ["rmote remote failed to start: ValueError: broken"]
    transport = ["Traceback (most recent call last):", "ValueError: broken"]

    assert BaseProtocol.start_failure([], []) == "Remote process closed the connection before PROTOCOL READY"
    assert "It said: rmote remote failed" in BaseProtocol.start_failure(said, [])
    assert "Its transport said: Traceback" in BaseProtocol.start_failure([], transport)
    both = BaseProtocol.start_failure(said, transport)
    assert "It said:" in both and "Its transport said:" in both
