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
