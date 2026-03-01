"""Integration tests for Exec tool - require docker."""

import subprocess

import pytest

from rmote.protocol import Protocol
from rmote.tools.exec import Exec

pytestmark = pytest.mark.timeout(60)


class TestExecLocal:
    """Unit tests that run Exec locally without Docker."""

    def test_command_success(self) -> None:
        result = Exec.command("true", check=False)
        assert result.returncode == 0

    def test_command_failure_no_check(self) -> None:
        result = Exec.command("false", check=False)
        assert result.returncode != 0

    def test_command_check_raises(self) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            Exec.command("false", check=True)

    def test_shell_success(self) -> None:
        result = Exec.shell("true", check=False)
        assert result.returncode == 0

    def test_shell_failure_no_check(self) -> None:
        result = Exec.shell("false", check=False)
        assert result.returncode != 0


@pytest.mark.asyncio
async def test_exec_command(docker_protocol: Protocol, subtests) -> None:
    with subtests.test("success returns rc 0"):
        result = await docker_protocol(Exec.command, "true")
        assert result.returncode == 0

    with subtests.test("check=True raises CalledProcessError on failure"):
        with pytest.raises(subprocess.CalledProcessError):
            await docker_protocol(Exec.command, "false")

    with subtests.test("check=False returns non-zero rc without raising"):
        result = await docker_protocol(Exec.command, "false", check=False)
        assert result.returncode != 0

    with subtests.test("explicit exit code via sh -c"):
        result = await docker_protocol(Exec.command, "sh", "-c", "exit 42", check=False)
        assert result.returncode == 42

    with subtests.test("cwd changes working directory"):
        # shell test [ $(pwd) = /tmp ] exits 0 iff cwd is /tmp
        await docker_protocol(Exec.shell, '[ "$(pwd)" = /tmp ]', cwd="/tmp")

    with subtests.test("env replaces environment"):
        # test -n "$MY_VAR" exits 0 iff MY_VAR is non-empty
        await docker_protocol(
            Exec.shell,
            'test -n "$MY_VAR"',
            env={"MY_VAR": "hello", "PATH": "/bin:/usr/bin"},
        )

    with subtests.test("stdin is provided to the process"):
        # python3 reads stdin and exits 0 iff it equals 'hello'
        await docker_protocol(
            Exec.command,
            "python3",
            "-c",
            "import sys; sys.exit(0 if sys.stdin.read().strip() == 'hello' else 1)",
            stdin="hello",
        )


@pytest.mark.asyncio
async def test_exec_shell(docker_protocol: Protocol, subtests) -> None:
    with subtests.test("shell success"):
        result = await docker_protocol(Exec.shell, "true")
        assert result.returncode == 0

    with subtests.test("shell failure raises"):
        with pytest.raises(subprocess.CalledProcessError):
            await docker_protocol(Exec.shell, "false")

    with subtests.test("shell check=False"):
        result = await docker_protocol(Exec.shell, "exit 7", check=False)
        assert result.returncode == 7

    with subtests.test("shell pipeline"):
        await docker_protocol(Exec.shell, "echo hello | grep hello")

    with subtests.test("shell stdin"):
        await docker_protocol(
            Exec.shell,
            'read line; [ "$line" = hello ]',
            stdin="hello\n",
        )
