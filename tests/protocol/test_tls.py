import asyncio
import ssl
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from rmote.protocol import BaseProtocol, Protocol, TLSConfig, TLSStream
from rmote.sync import Connection
from tests.support.tls_tools import TLSMethods
from tests.sync.tools import Methods

CERTS = Path(__file__).parents[1] / "fixtures" / "tls"


@pytest.fixture
def tls_config() -> TLSConfig:
    return TLSConfig(
        certfile=str(CERTS / "controller.crt"),
        keyfile=str(CERTS / "controller.key"),
        cafile=str(CERTS / "controller.crt"),
        server_hostname="rmote.test",
    )


@asynccontextmanager
async def tls_pair(config: TLSConfig) -> AsyncIterator[tuple[TLSStream, TLSStream]]:
    accepted: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = asyncio.Queue()
    server = await asyncio.start_server(lambda r, w: accepted.put_nowait((r, w)), "127.0.0.1", 0)
    client_reader, client_writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    server_reader, server_writer = await accepted.get()
    context, ca_data = config.prepare()
    client_context = ssl.create_default_context(cadata=ca_data)
    client_context.minimum_version = ssl.TLSVersion.TLSv1_3
    controller = TLSStream(server_reader, server_writer, context, server_side=True)
    peer = TLSStream(
        client_reader, client_writer, client_context, server_side=False, server_hostname=config.server_hostname
    )
    try:
        await asyncio.gather(controller.handshake(2), peer.handshake(2))
        yield controller, peer
    finally:
        controller.close()
        peer.close()
        await asyncio.gather(controller.wait_closed(), peer.wait_closed(), return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_subprocess_tls_encrypts_boundaries_headers_and_payload(tls_config, monkeypatch):
    protocol = await Protocol.from_command(python=sys.executable, tls=tls_config)
    wire = bytearray()
    raw_write = protocol.writer.write

    def capture(data):
        wire.extend(data)
        raw_write(data)

    monkeypatch.setattr(protocol.writer, "write", capture)
    async with protocol:
        assert isinstance(protocol.writer, TLSStream)
        assert protocol.writer.ssl_object.version() == "TLSv1.3"
        payload = b"rmote-private-payload-unique" * 20000
        assert await protocol(TLSMethods.echo, payload) == payload
        assert BaseProtocol.BOUNDARY not in wire
        assert BaseProtocol.MAGIC not in wire
        assert b"rmote-private-payload-unique" not in wire
    assert protocol._owned_process is None
    assert protocol.writer._pump is not None
    assert protocol.writer._pump.done()


@pytest.mark.asyncio
async def test_tls_concurrent_fragmented_rpc_and_streaming(tls_config):
    import os

    async with await Protocol.from_command(python=sys.executable, tls=tls_config) as protocol:
        payloads = [os.urandom(180000) for _ in range(8)]
        results = await asyncio.gather(*(protocol(TLSMethods.echo, data) for data in payloads))
        assert results == payloads
        received = [item async for item in protocol(TLSMethods.chunks, 40, 100000)]
        assert received == [bytes([i]) * 100000 for i in range(40)]


def test_sync_tls_rpc_and_cleanup(tls_config):
    with Connection.from_local(tls=tls_config) as remote:
        assert remote(Methods.echo, "TLS sync") == "TLS sync"
        protocol = remote._protocol
        assert protocol is not None
        assert isinstance(protocol.writer, TLSStream)
        assert protocol.writer.ssl_object.version() == "TLSv1.3"
        process = remote._process
        assert process is not None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_tls_rejects_wrong_name_and_reaps_process(tls_config):
    protocol = await Protocol.from_command(
        python=sys.executable, tls=replace(tls_config, server_hostname="wrong.test")
    )
    process = protocol._owned_process
    assert process is not None
    async with asyncio.timeout(5):
        with pytest.raises(ssl.SSLError):
            async with protocol:
                pytest.fail("TLS accepted the wrong server name")
    assert process.returncode is not None
    assert protocol._loop_task is None


@pytest.mark.asyncio
async def test_tls_rejects_untrusted_certificate(tls_config):
    config = replace(tls_config, cafile=str(CERTS / "untrusted.crt"))
    protocol = await Protocol.from_command(python=sys.executable, tls=config)
    async with asyncio.timeout(5):
        with pytest.raises(ssl.SSLError):
            async with protocol:
                pytest.fail("TLS accepted an untrusted server")
    assert protocol._owned_process is None


@pytest.mark.asyncio
async def test_tls_record_fragmentation_and_clean_eof(tls_config, monkeypatch):
    async with tls_pair(tls_config) as (controller, peer):
        original = controller._raw_writer.write

        def fragment(data):
            for offset in range(0, len(data), 7):
                original(data[offset : offset + 7])

        monkeypatch.setattr(controller._raw_writer, "write", fragment)
        controller.write(b"hello")
        controller.write(b" world\n")
        await controller.drain()
        assert await peer.reader.readline() == b"hello world\n"
        controller.close()
        assert await peer.reader.read() == b""


@pytest.mark.asyncio
async def test_tls_tampered_record_is_rejected(tls_config, monkeypatch):
    async with tls_pair(tls_config) as (controller, peer):
        original = controller._raw_writer.write

        def corrupt(data):
            damaged = bytearray(data)
            damaged[-1] ^= 1
            original(damaged)

        monkeypatch.setattr(controller._raw_writer, "write", corrupt)
        controller.write(b"must not reach the application")
        await controller.drain()
        with pytest.raises(ssl.SSLError):
            await peer.reader.readexactly(1)
        assert peer.is_closing()


@pytest.mark.asyncio
async def test_tls_abrupt_eof_is_not_a_clean_shutdown(tls_config):
    async with tls_pair(tls_config) as (controller, peer):
        controller._raw_writer.close()
        with pytest.raises(ssl.SSLEOFError):
            await peer.reader.readexactly(1)


@pytest.mark.asyncio
async def test_tls_receive_backpressure_resumes(tls_config):
    async with tls_pair(tls_config) as (controller, peer):
        payload = b"x" * (1024 * 1024)
        controller.write(payload)
        sending = asyncio.create_task(controller.drain())
        try:
            async with asyncio.timeout(3):
                while peer.is_reading():
                    await asyncio.sleep(0.001)
            assert len(peer.reader._buffer) <= 2 * 65536 + 16384  # type: ignore[attr-defined]
            assert await peer.reader.readexactly(len(payload)) == payload
            await sending
        finally:
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
async def test_tls_full_duplex_under_write_backpressure(tls_config):
    async with tls_pair(tls_config) as (controller, peer):
        outgoing = b"a" * (4 * 1024 * 1024)
        incoming = b"b" * len(outgoing)
        controller.write(outgoing)
        peer.write(incoming)
        async with asyncio.timeout(3):
            _, _, at_controller, at_peer = await asyncio.gather(
                controller.drain(),
                peer.drain(),
                controller.reader.readexactly(len(incoming)),
                peer.reader.readexactly(len(outgoing)),
            )
        assert at_controller == incoming
        assert at_peer == outgoing


@pytest.mark.asyncio
async def test_tls_close_is_bounded_when_peer_stops_reading(tls_config):
    async with tls_pair(tls_config) as (controller, peer):
        peer.pause_reading()
        controller._raw_writer.transport.set_write_buffer_limits(high=4096)
        controller.write(b"x" * (16 * 1024 * 1024))
        sending = asyncio.create_task(controller.drain())
        try:
            async with asyncio.timeout(3):
                while controller._raw_writer.transport.get_write_buffer_size() == 0:
                    await asyncio.sleep(0.001)
            controller.close()
            async with asyncio.timeout(2):
                await controller.wait_closed()
            assert controller.is_closing()
        finally:
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_tls_interrupted_handshake_closes_transport(tls_config, cancel):
    accepted: asyncio.Queue[asyncio.StreamWriter] = asyncio.Queue()
    server = await asyncio.start_server(lambda r, w: accepted.put_nowait(w), "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    remote_writer = await accepted.get()
    context, _ = tls_config.prepare()
    stream = TLSStream(reader, writer, context, server_side=True)
    try:
        if cancel:
            handshaking = asyncio.create_task(stream.handshake(5))
            await asyncio.sleep(0)
            handshaking.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handshaking
        else:
            with pytest.raises(TimeoutError):
                await stream.handshake(0.05)
        assert stream.is_closing()
        await stream.wait_closed()
    finally:
        remote_writer.close()
        await remote_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_invalid_tls_configuration_reaps_spawned_process(tls_config, monkeypatch, tmp_path):
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(FileNotFoundError):
        await Protocol.from_command(
            python=sys.executable, tls=replace(tls_config, keyfile=str(tmp_path / "missing.key"))
        )
    assert len(processes) == 1
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_plaintext_connection_still_works():
    async with await Protocol.from_command(python=sys.executable) as protocol:
        assert not isinstance(protocol.writer, TLSStream)
        assert await protocol(Methods.echo, "plain") == "plain"
