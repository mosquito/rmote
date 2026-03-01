"""Verify that the remote process cannot corrupt the protocol channel.

The remote interpreter communicates with the local side over its own fd 0
(stdin) and a duped stdout fd.  Before the fix in ``from_stdio``, any tool
method that wrote to fd 1 - via ``subprocess.run`` with inherited stdio,
``os.write(1, ...)``, or Python's ``print()`` - would inject arbitrary bytes
into the binary packet stream and permanently break the connection.

Each sub-test deliberately performs one of these "poisoning" writes and then
asserts that the protocol is still alive and responsive afterwards.
"""

import pytest

from rmote.protocol import Protocol, Tool

pytestmark = pytest.mark.timeout(30)


async def _poison_cases(proto: Protocol, subtests) -> None:
    """Run all poisoning cases against *proto*.

    Shared between the local-subprocess and docker variants so both transports
    are covered by the same set of assertions.
    """

    with subtests.test("subprocess.run with inherited stdout"):
        # subprocess inherits fd 1 from the remote process; before the fix it
        # would write "POISON\n" directly onto the protocol pipe.
        class SubprocStdout(Tool):
            @staticmethod
            def run() -> str:
                import subprocess

                subprocess.run(["echo", "POISON_STDOUT"])
                return "ok"

        assert await proto(SubprocStdout.run) == "ok"

    with subtests.test("subprocess.run with inherited stderr"):
        # Same for fd 2 - stderr was also the protocol channel before.
        class SubprocStderr(Tool):
            @staticmethod
            def run() -> str:
                import subprocess

                subprocess.run(["sh", "-c", "echo POISON_STDERR >&2"])
                return "ok"

        assert await proto(SubprocStderr.run) == "ok"

    with subtests.test("os.write(1, ...) direct fd write"):
        # Direct OS-level write bypasses Python buffering entirely.
        class OsWriteStdout(Tool):
            @staticmethod
            def run() -> str:
                import os

                os.write(1, b"POISON_FD1\n")
                return "ok"

        assert await proto(OsWriteStdout.run) == "ok"

    with subtests.test("os.write(2, ...) direct fd write"):

        class OsWriteStderr(Tool):
            @staticmethod
            def run() -> str:
                import os

                os.write(2, b"POISON_FD2\n")
                return "ok"

        assert await proto(OsWriteStderr.run) == "ok"

    with subtests.test("print() in tool body"):
        # Python's print() goes through sys.stdout → fd 1.
        class PrintTool(Tool):
            @staticmethod
            def run() -> str:
                print("POISON_PRINT")
                return "ok"

        assert await proto(PrintTool.run) == "ok"

    with subtests.test("concurrent poisoning calls"):
        # Multiple concurrent sub-processes all writing to stdout at once.
        import asyncio

        class ConcurrentPoison(Tool):
            @staticmethod
            def run(n: int) -> int:
                import subprocess

                subprocess.run(["sh", "-c", f"echo CONCURRENT_{n}"])
                return n

        results = await asyncio.gather(*[proto(ConcurrentPoison.run, i) for i in range(5)])
        assert sorted(results) == [0, 1, 2, 3, 4]

    with subtests.test("protocol still live after all poison attempts"):
        # Final sanity check: the channel must still carry normal traffic.
        class Ping(Tool):
            @staticmethod
            def ping() -> str:
                return "pong"

        assert await proto(Ping.ping) == "pong"


@pytest.mark.asyncio
async def test_stdio_protection_local(protocol: Protocol, subtests) -> None:
    await _poison_cases(protocol, subtests)


@pytest.mark.asyncio
async def test_stdio_protection_docker(docker_protocol: Protocol, subtests) -> None:
    await _poison_cases(docker_protocol, subtests)
