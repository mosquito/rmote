"""A peer that never reaches the ready boundary must explain itself.

The transport discards the peer's stderr by default, so whatever the peer
wrote to the protocol stream is the only evidence the caller has.
"""

import asyncio
import sys
from typing import Any, cast

import pytest

from rmote.protocol import BaseProtocol, Protocol

pytestmark = pytest.mark.timeout(60)


def reading(*chunks: bytes) -> BaseProtocol:
    """Build a protocol whose stream holds *chunks* and then ends."""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return BaseProtocol(reader, cast(Any, None))


def test_a_silent_peer_gives_the_plain_reason():
    assert BaseProtocol.start_failure([]) == "Remote process closed the connection before PROTOCOL READY"


def test_the_reason_carries_what_the_peer_said():
    reason = BaseProtocol.start_failure(["ModuleNotFoundError: No module named _lzma"])
    assert reason.startswith("Remote process closed the connection before PROTOCOL READY")
    assert "No module named _lzma" in reason


def test_the_reason_keeps_its_tail():
    # The cause of a traceback is its last line, so the tail survives.
    reason = BaseProtocol.start_failure(["x" * 5000, "the real cause"])
    assert "the real cause" in reason
    assert len(reason) < 5000


@pytest.mark.asyncio
async def test_the_boundary_reports_what_arrived_before_the_end():
    protocol = reading(b"disk is full\n", b"giving up\n")
    with pytest.raises(ConnectionError) as info:
        await protocol.read_boundary()
    assert "disk is full" in str(info.value)
    assert "giving up" in str(info.value)


@pytest.mark.asyncio
async def test_noise_before_the_boundary_does_not_fail_the_handshake():
    protocol = reading(b"a warning\n", BaseProtocol.BOUNDARY)
    await protocol.read_boundary()


@pytest.mark.asyncio
async def test_a_chatty_peer_cannot_build_a_huge_reason():
    lines = [f"line {index}\n".encode() for index in range(200)]
    protocol = reading(*lines)
    with pytest.raises(ConnectionError) as info:
        await protocol.read_boundary()
    reason = str(info.value)
    assert len(reason) <= BaseProtocol.MAX_START_FAILURE_TEXT + 200
    # The limit keeps the last lines, which hold the cause.
    assert "line 199" in reason
    assert "line 0\n" not in reason


@pytest.mark.asyncio
async def test_blank_lines_add_nothing_to_the_reason():
    protocol = reading(b"\n", b"   \n", b"\n")
    with pytest.raises(ConnectionError) as info:
        await protocol.read_boundary()
    assert str(info.value) == "Remote process closed the connection before PROTOCOL READY"


@pytest.mark.asyncio
async def test_a_transport_that_speaks_and_dies_reports_its_words():
    # The transport replaces the interpreter, so it never reaches the boundary.
    with pytest.raises(ConnectionError, match="disk is full"):
        async with await Protocol.from_command("sh", "-c", "echo 'disk is full'; exit 1", python=sys.executable):
            pass


@pytest.mark.asyncio
async def test_a_silent_transport_still_reports_the_symptom():
    with pytest.raises(ConnectionError, match="before PROTOCOL READY"):
        async with await Protocol.from_command("sh", "-c", "exit 1", python=sys.executable):
            pass


@pytest.mark.asyncio
async def test_a_working_connection_is_unaffected():
    async with await Protocol.from_command(python=sys.executable) as remote:
        assert remote is not None
