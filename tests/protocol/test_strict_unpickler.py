"""Strict decoding rejects executable globals and cached extension references."""

import builtins
import copyreg
import io
import pickle
from typing import Any

import pytest

from rmote.protocol import StrictUnpickler


def restore_record(value):
    if type(value) is not int:
        raise ValueError("Record value must be an integer")
    return {"value": value}


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_builtin_data_roundtrip(protocol):
    value: list[Any] = [None, True, 42, 1.5, "text", {"key": [1, 2]}, (1, 2)]
    if protocol >= 3:
        value += [{1, 2}, frozenset({3}), 1j, range(3), b"bytes", bytearray(b"buffer")]
    assert StrictUnpickler.loads(pickle.dumps(value, protocol=protocol)) == value


@pytest.mark.parametrize("error", [PermissionError, ValueError, RuntimeError, TypeError, FileNotFoundError])
def test_portable_errors_can_be_decoded(error):
    restored = StrictUnpickler.loads(pickle.dumps(error("refused")))
    assert type(restored) is error
    assert restored.args == ("refused",)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_reduce_never_executes_unknown_callable(tmp_path, protocol):
    marker = tmp_path / "executed"

    class Evil:
        def __reduce__(self):
            return eval, (f"open({str(marker)!r}, 'w').close()",)

    with pytest.raises(PermissionError, match="eval"):
        StrictUnpickler.loads(pickle.dumps(Evil(), protocol=protocol))
    assert not marker.exists()


def test_explicit_global_does_not_import_its_module(monkeypatch):
    imports = []
    original = builtins.__import__

    def observed(name, *args, **kwargs):
        imports.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observed)
    with pytest.raises(PermissionError, match="peer_module.function"):
        StrictUnpickler.loads(b"cpeer_module\nfunction\n.")
    assert "peer_module" not in imports


def test_explicit_reconstructor_validates_hostile_arguments():
    class Record:
        def __init__(self, value: Any) -> None:
            self.value = value

        def __reduce__(self):
            return restore_record, (self.value,)

    allowed = {(__name__, "restore_record"): restore_record}
    with pytest.raises(PermissionError):
        StrictUnpickler.loads(pickle.dumps(Record(42)))
    assert StrictUnpickler.loads(pickle.dumps(Record(42)), allowed_globals=allowed) == {"value": 42}
    with pytest.raises(ValueError, match="integer"):
        StrictUnpickler.loads(pickle.dumps(Record("bad")), allowed_globals=allowed)


@pytest.mark.parametrize("code", [241, 4097, 16777217], ids=["ext1", "ext2", "ext4"])
def test_cached_extensions_cannot_bypass_find_class(tmp_path, code):
    marker = tmp_path / "executed"

    class Evil:
        def __reduce__(self):
            return eval, (f"open({str(marker)!r}, 'w').close()",)

    copyreg.add_extension("builtins", "eval", code)
    try:
        payload = pickle.dumps(Evil(), protocol=2)
        vars(copyreg)["_extension_cache"][code] = eval
        with pytest.raises(PermissionError, match="EXT"):
            StrictUnpickler.loads(payload)
        assert not marker.exists()
    finally:
        copyreg.remove_extension("builtins", "eval", code)


@pytest.mark.parametrize("payload", [b"Ppeer-reference\n.", b"\x80\x02X\x01\x00\x00\x00xQ."], ids=["text", "binary"])
def test_persistent_references_are_forbidden(payload):
    with pytest.raises(PermissionError, match="PERSID"):
        StrictUnpickler.loads(payload)


def test_opcode_bytes_inside_data_are_not_instructions():
    value = bytes(range(256))
    assert StrictUnpickler.loads(pickle.dumps(value)) == value


def test_trailing_pickle_cannot_be_loaded_after_validation():
    with pytest.raises(PermissionError, match="Trailing"):
        StrictUnpickler(io.BytesIO(pickle.dumps(42) + pickle.dumps(eval)))


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [-1, 1], ids=["inline", "worker"])
@pytest.mark.parametrize("stream", [False, True], ids=["rpc", "stream"])
async def test_strict_rpc_rejects_reduce_and_keeps_the_connection(tmp_path, transport_tls, threshold, stream):
    import sys

    from rmote.protocol import Protocol
    from tests.support.tool_cases.permissions import StrictProbe

    marker = tmp_path / "executed"
    async with await Protocol.from_command(
        python=sys.executable, tls=transport_tls, pickle_allowlist={}, decode_threshold=threshold
    ) as remote:
        with pytest.raises(PermissionError, match="eval"):
            if stream:
                async for _ in remote(StrictProbe.stream, str(marker), b"padding" * 30000):
                    pytest.fail("An unsafe item must not reach the consumer")
            else:
                await remote(StrictProbe.evil, str(marker), b"padding" * 30000)
        assert not marker.exists()
        assert await remote(StrictProbe.echo) == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [False, True], ids=["denied", "explicit"])
async def test_explicit_class_permission_is_local(transport_tls, allow):
    import sys
    from decimal import Decimal

    from rmote.protocol import Protocol
    from tests.support.tool_cases.permissions import StrictProbe

    allowed = {("decimal", "Decimal"): Decimal} if allow else {}
    async with await Protocol.from_command(
        python=sys.executable, tls=transport_tls, pickle_allowlist=allowed
    ) as remote:
        if allow:
            assert await remote(StrictProbe.decimal) == Decimal("1.25")
        else:
            with pytest.raises(PermissionError, match="Decimal"):
                await remote(StrictProbe.decimal)
        assert await remote(StrictProbe.echo) == 42


def test_allowlist_cannot_enable_official_tool_sources(tmp_path):
    from rmote.protocol import PacketUnpickler, tool_from_dict

    marker = tmp_path / "executed"

    class Payload:
        def __reduce__(self):
            return tool_from_dict, ({"name": "Bad", "source": f"open({str(marker)!r}, 'w').close()"},)

    with pytest.raises(PermissionError, match="Tool sources"):
        PacketUnpickler.loads(
            pickle.dumps(Payload()), pickle_allowlist={("rmote.protocol", "tool_from_dict"): tool_from_dict}
        )
    assert not marker.exists()


def test_sync_factory_enables_strict_decoding(transport_tls, tmp_path):
    from rmote.sync import Connection
    from tests.support.tool_cases.permissions import StrictProbe

    marker = tmp_path / "executed"
    with Connection.from_local(tls=transport_tls, pickle_allowlist={}) as connection:
        with pytest.raises(PermissionError, match="eval"):
            connection(StrictProbe.evil, str(marker), b"")
        assert connection(StrictProbe.echo) == 42
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["packet", "batch", "envelope", "enclosed"])
@pytest.mark.parametrize("threshold", [-1, 1], ids=["inline", "worker"])
async def test_every_decode_surface_uses_the_allowlist(tmp_path, surface, threshold):
    from rmote.protocol import BaseProtocol, Flags
    from tests.protocol.test_receive_workers import receiver

    marker = tmp_path / "executed"

    class Payload:
        def __reduce__(self):
            return eval, (f"open({str(marker)!r}, 'w').close()",)

    payload = pickle.dumps([Payload(), b"padding" * 30000])
    flags = Flags.RPC | Flags.RESPONSE
    if surface == "batch":
        payload = BaseProtocol.batch([payload])
        flags |= Flags.STREAM | Flags.BATCH
    elif surface in {"envelope", "enclosed"}:
        flags |= Flags.MODULES
        if surface == "enclosed":
            payload = pickle.dumps(({}, payload))
    instance = receiver([(payload, flags), (pickle.dumps(42), Flags.RPC)], decode_threshold=threshold)
    instance.accept_code = False
    instance.pickle_allowlist = None
    packet = await instance.receive()
    assert packet.flags == Flags.EXCEPTION | Flags.RESPONSE
    assert isinstance(packet.payload, PermissionError)
    assert not marker.exists()
    assert (await instance.receive()).payload == 42
    assert not instance.fragments.buffers


@pytest.mark.asyncio
async def test_strict_request_returns_permission_error_without_execution(tmp_path):
    from rmote.protocol import Flags
    from tests.protocol.test_permissions import connected, ping

    marker = tmp_path / "executed"

    class Payload:
        def __reduce__(self):
            return eval, (f"open({str(marker)!r}, 'w').close()",)

    async with connected() as (left, right):
        left.pickle_allowlist = {}
        left.accept_calls = True
        with pytest.raises(PermissionError, match="eval"):
            await right._call(Payload(), Flags.RPC | Flags.REQUEST)
        assert not marker.exists()
        assert await ping(left) == 42


def test_code_permission_bypasses_strict_decoder_even_with_allowlist(monkeypatch):
    from rmote.protocol import PacketUnpickler

    def refused(*args, **kwargs):
        raise AssertionError("Trusted code receiver must not use StrictUnpickler")

    monkeypatch.setattr(StrictUnpickler, "loads", refused)
    assert PacketUnpickler.loads(pickle.dumps(42), accept_code=True, pickle_allowlist={}) == 42


@pytest.mark.parametrize("allowlist", [None, {}])
def test_no_code_permission_cannot_disable_strict_decoding(allowlist):
    from rmote.protocol import PacketUnpickler

    with pytest.raises(PermissionError, match="eval"):
        PacketUnpickler.loads(pickle.dumps(eval), accept_code=False, pickle_allowlist=allowlist)


@pytest.mark.asyncio
async def test_runtime_code_permission_selects_decoder():
    from rmote.protocol import Flags
    from tests.protocol.test_receive_workers import receiver

    payload = pickle.dumps(eval)
    instance = receiver([(payload, Flags.RPC), (payload, Flags.RPC), (payload, Flags.RPC)])
    instance.accept_code = False
    assert isinstance((await instance.receive()).payload, PermissionError)
    instance.accept_code = True
    assert (await instance.receive()).payload is eval
    instance.accept_code = False
    assert isinstance((await instance.receive()).payload, PermissionError)
