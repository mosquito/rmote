"""Pickle permissions come from explicit local registration."""

import pickle
from dataclasses import dataclass
from typing import Any

import pytest

from rmote.protocol import REGISTERED_GLOBALS, PacketUnpickler, StrictUnpickler, register
from rmote.tools.apt import Package as AptPackage
from rmote.tools.pacman import Package as PacmanPackage


@dataclass
class Record:
    value: int


class OtherRecord(Record):
    pass


def restore_record(value):
    if type(value) is not int:
        raise ValueError("Expected an integer")
    return Record(value)


class ReducedRecord:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __reduce__(self):
        return restore_record, (self.value,)


@pytest.fixture(autouse=True)
def restore_permissions():
    original = REGISTERED_GLOBALS.copy()
    yield
    REGISTERED_GLOBALS.clear()
    REGISTERED_GLOBALS.update(original)


def test_registration_returns_the_same_global_and_allows_its_instances():
    data = pickle.dumps(Record(42))
    with pytest.raises(PermissionError, match="Record"):
        StrictUnpickler.loads(data)
    assert register(Record) is Record
    assert PacketUnpickler.loads(data) == Record(42)
    assert register(Record) is Record
    with pytest.raises(PermissionError, match="OtherRecord"):
        StrictUnpickler.loads(pickle.dumps(OtherRecord(1)))


def test_registered_reconstructor_validates_peer_arguments():
    register(restore_record)
    assert StrictUnpickler.loads(pickle.dumps(ReducedRecord(42))) == Record(42)
    with pytest.raises(ValueError, match="integer"):
        StrictUnpickler.loads(pickle.dumps(ReducedRecord("bad")))


@pytest.mark.parametrize("value", [None, "os.system", 42])
def test_registration_requires_a_local_global(value: Any):
    with pytest.raises(TypeError):
        register(value)


def test_local_definition_cannot_be_registered_as_an_importable_global():
    class Local:
        pass

    with pytest.raises(ValueError, match="importable global"):
        register(Local)


@pytest.mark.parametrize("value", [AptPackage("example"), PacmanPackage("example")])
def test_builtin_models_include_permissions_for_nested_enum_values(value):
    restored = PacketUnpickler.loads(pickle.dumps(value))
    assert restored == value
    assert type(restored.state) is type(value.state)
