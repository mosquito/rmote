"""Transferred modules must exist while dataclass decorators execute."""

import sys
from typing import Any, cast

import pytest

from rmote.protocol import tool_from_dict


@pytest.mark.parametrize("failure", [False, True])
def test_postponed_dataclass_annotations_and_module_rollback(failure):
    name = f"rmote_dataclass_transfer_probe_{failure}"
    source = """from __future__ import annotations
from dataclasses import dataclass
from rmote.protocol import Tool
@dataclass(slots=True)
class Record:
    value: int
class Probe(Tool):
    @staticmethod
    def collect() -> Record:
        return Record(42)
"""
    if failure:
        source += '\nraise RuntimeError("failed module")\n'
    try:
        if failure:
            with pytest.raises(RuntimeError, match="failed module"):
                tool_from_dict(
                    {
                        "name": "Probe",
                        "module": name,
                        "sources": {name: {"source": source, "file": "<probe>", "package": False}},
                    }
                )
            assert name not in sys.modules
        else:
            probe = tool_from_dict(
                {
                    "name": "Probe",
                    "module": name,
                    "sources": {name: {"source": source, "file": "<probe>", "package": False}},
                }
            )
            assert cast(Any, probe).collect().value == 42
            assert sys.modules[name].Record.__module__ == name
    finally:
        sys.modules.pop(name, None)
