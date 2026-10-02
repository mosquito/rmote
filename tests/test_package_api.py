"""Verify public imports and typing from installed distribution artifacts."""

import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

TOOLS = """\
from dataclasses import dataclass
from rmote.protocol import Tool

@dataclass
class Result:
    value: int

class Checks(Tool):
    @staticmethod
    def value(number: int) -> int:
        return number

    @staticmethod
    async def report(number: int) -> Result:
        return Result(number)
"""

SMOKE = """\
import asyncio
import importlib.metadata
import threading

original_start = threading.Thread.start
original_loop = asyncio.new_event_loop
original_events_loop = asyncio.events.new_event_loop

def unexpected_resource(*args, **kwargs):
    raise AssertionError("Import must not start a thread or create a loop")

threading.Thread.start = unexpected_resource
asyncio.new_event_loop = unexpected_resource
asyncio.events.new_event_loop = unexpected_resource
from rmote.sync import Connection
import rmote.sync
assert rmote.sync.__all__ == ["Connection"]
namespace = {}
exec("from rmote.sync import *", namespace)
assert set(namespace) - {"__builtins__"} == {"Connection"}
assert not importlib.metadata.distribution("rmote").requires
threading.Thread.start = original_start
asyncio.new_event_loop = original_loop
asyncio.events.new_event_loop = original_events_loop

from probe_tools import Checks, Result
with Connection.from_local() as remote:
    assert remote(Checks.value, 7) == 7
    assert remote.call_with_timeout(5.0, Checks.report, 8) == Result(8)

from rmote.protocol import Protocol
async def asynchronous():
    process = await asyncio.create_subprocess_exec(
        __import__("sys").executable, "-qui", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        protocol = await Protocol.from_subprocess(process)
        async with protocol:
            assert await protocol(Checks.value, 9) == 9
            assert await protocol(Checks.report, 10) == Result(10)
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()
asyncio.run(asynchronous())
"""

TYPED_USAGE = """\
from typing import assert_type
from rmote.sync import Connection
from probe_tools import Checks, Result

def check(remote: Connection) -> None:
    assert_type(remote(Checks.value, 7), int)
    assert_type(remote(Checks.report, 8), Result)
    assert_type(remote.call_with_timeout(None, Checks.report, 9), Result)
    assert_type(Connection.from_local(), Connection)
    with Connection.from_local() as managed:
        assert_type(managed, Connection)
"""


def run(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="module")
def distribution_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build and install package artifacts")
    directory = tmp_path_factory.mktemp("distribution-artifacts")
    run([uv, "build", "--out-dir", str(directory), "--no-create-gitignore"], ROOT)
    return {"wheel": next(directory.glob("*.whl")), "sdist": next(directory.glob("*.tar.gz"))}


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_installed_distribution_exports_types_and_runs_both_clients(
    kind: str,
    distribution_artifacts: dict[str, Path],
    tmp_path: Path,
) -> None:
    artifact = distribution_artifacts[kind]
    expected = {"rmote/sync.py", "rmote/_runtime.py", "rmote/protocol.py", "rmote/py.typed"}
    if kind == "wheel":
        with zipfile.ZipFile(artifact) as archive:
            names = set(archive.namelist())
    else:
        with tarfile.open(artifact) as archive:
            names = {name.partition("/")[2] for name in archive.getnames()}
    assert expected <= names

    uv = shutil.which("uv")
    assert uv is not None
    environment = tmp_path / "environment"
    run([uv, "venv", "--python", sys.executable, str(environment)], tmp_path)
    python = environment / "bin" / "python"
    run([uv, "pip", "install", "--python", str(python), "--no-deps", str(artifact)], tmp_path)
    (tmp_path / "probe_tools.py").write_text(textwrap.dedent(TOOLS))
    (tmp_path / "probe.py").write_text(textwrap.dedent(SMOKE))
    run([str(python), "probe.py"], tmp_path)

    # Resolve imports from the clean installed environment, outside the repo.
    usage = tmp_path / "typed_usage.py"
    usage.write_text(textwrap.dedent(TYPED_USAGE))
    run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--python-executable",
            str(python),
            str(usage),
        ],
        tmp_path,
    )


def test_protocol_import_does_not_load_sync_runtime(tmp_path: Path) -> None:
    script = """\
import sys
import rmote.protocol
assert "rmote.sync" not in sys.modules
assert "rmote._runtime" not in sys.modules
"""
    run([sys.executable, "-I", "-c", textwrap.dedent(script)], tmp_path)


def test_protocol_runs_as_a_standalone_module(tmp_path: Path) -> None:
    script = """\
import runpy
import sys
runpy.run_path(sys.argv[1])
assert "rmote" not in sys.modules
assert "rmote.sync" not in sys.modules
assert "rmote._runtime" not in sys.modules
"""
    run([sys.executable, "-I", "-c", textwrap.dedent(script), str(ROOT / "rmote" / "protocol.py")], tmp_path)
