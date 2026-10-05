"""Serialization must release its buffers without waiting for cyclic GC."""

import gc
import io
import weakref
from collections.abc import Iterator
from typing import Any, cast

import pytest

from rmote.protocol import BaseProtocol, ModulePickler


class Payload:
    def __init__(self) -> None:
        self.data = b"x" * (1024 * 1024)


class Unpicklable:
    def __reduce__(self) -> Any:
        raise TypeError("Cannot serialize this value")


@pytest.fixture
def no_cyclic_gc() -> Iterator[None]:
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        gc.collect()
        if enabled:
            gc.enable()


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_dump_releases_pickler_stream_and_payload(no_cyclic_gc, fail: bool) -> None:
    def dump() -> tuple[weakref.ReferenceType[Any], ...]:
        stream = io.BytesIO()
        pickler = ModulePickler(stream, set())
        payload = Payload()
        refs = weakref.ref(pickler), weakref.ref(stream), weakref.ref(payload)
        if fail:
            # Serialize the large payload before the next value fails.
            with pytest.raises(TypeError, match="Cannot serialize this value"):
                pickler.dump([payload, Unpicklable()])
        else:
            pickler.dump(payload)
        return refs

    refs = dump()
    assert all(ref() is None for ref in refs)


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_encode_releases_payload(no_cyclic_gc, fail: bool) -> None:
    protocol = BaseProtocol(cast(Any, None), cast(Any, None))

    def encode() -> weakref.ReferenceType[Payload]:
        payload = Payload()
        ref = weakref.ref(payload)
        if fail:
            with pytest.raises(TypeError, match="Cannot serialize this value"):
                protocol.encode([payload, Unpicklable()])
        else:
            protocol.encode(payload)
        return ref

    ref = encode()
    assert ref() is None
