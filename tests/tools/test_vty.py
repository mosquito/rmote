"""Tests for the Vty tool.

The tool runs a real child on the remote side, with a pseudo terminal or with
pipes, and its output arrives through a streaming call.
"""

import asyncio
import os
import sys
from typing import Any

import pytest

from rmote.protocol import Protocol
from rmote.tools.vty import Session, Vty, launcher_source

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX host")

TIMEOUT = 20


def open_descriptors() -> set[str]:
    """Return the descriptors this process holds.

    /dev/fd lists them on macOS and on Linux, where it points at /proc/self/fd.
    """
    return set(os.listdir("/dev/fd"))


async def collect(protocol: Protocol, key: int, marker: bytes | None = None) -> bytes:
    """Read the output of a session, up to *marker* or up to its end."""
    out = bytearray()
    async with asyncio.timeout(TIMEOUT):
        async for chunk in protocol(Vty.output, key):
            out += chunk
            if marker is not None and marker in out:
                break
    return bytes(out)


async def open_terminal(protocol: Protocol, *argv: str, **kwargs: Any) -> int:
    """Open a terminal session, with the launcher produced on this side."""
    return int(await protocol(Vty.open, list(argv), launcher=launcher_source(), **kwargs))


class TestTerminalSession:
    @pytest.mark.asyncio
    async def test_output_streams_and_status_is_reported(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "echo VTY_MARKER_OK; exit 3")

        output = await collect(protocol, key)
        assert b"VTY_MARKER_OK" in output
        assert await protocol(Vty.close, key) == 3

    @pytest.mark.asyncio
    async def test_child_gets_a_terminal(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "tty")

        output = await collect(protocol, key)
        assert b"/dev/" in output
        assert b"not a tty" not in output
        await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_window_size_follows_the_request(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "stty size", rows=40, cols=100)

        assert b"40 100" in await collect(protocol, key)
        await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_resize_reaches_the_terminal(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", rows=24, cols=80)
        try:
            await protocol(Vty.write, key, b"stty size\n")
            assert b"24 80" in await collect(protocol, key, b"24 80")

            await protocol(Vty.resize, key, 50, 120)
            await protocol(Vty.write, key, b"stty size\n")
            assert b"50 120" in await collect(protocol, key, b"50 120")
        finally:
            await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_term_and_environment_reach_the_child(self, protocol: Protocol) -> None:
        key = await open_terminal(
            protocol,
            "/bin/sh",
            "-c",
            "echo TERM_IS=$TERM VALUE=$RMOTE_VTY_TEST",
            term="xterm-256color",
            env={"RMOTE_VTY_TEST": "sentinel"},
        )

        output = await collect(protocol, key)
        assert b"TERM_IS=xterm-256color" in output
        assert b"VALUE=sentinel" in output
        await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_working_directory_applies(self, protocol: Protocol, tmp_path) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "pwd", cwd=str(tmp_path))

        # macOS resolves /tmp through a symlink, so compare the trailing name.
        assert tmp_path.name.encode() in await collect(protocol, key)
        await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_bulk_output_arrives_whole(self, protocol: Protocol) -> None:
        size = 400_000
        key = await open_terminal(
            protocol, "/bin/sh", "-c", f"dd if=/dev/zero bs=1000 count={size // 1000} 2>/dev/null"
        )

        output = await collect(protocol, key)
        assert output.count(b"\x00") == size
        await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_missing_command_reports_through_the_terminal(self, protocol: Protocol) -> None:
        """The launcher reports the failure and leaves the shell exit status."""
        key = await open_terminal(protocol, "/nonexistent/rmote-vty-binary")

        output = await collect(protocol, key)
        assert b"cannot run" in output
        assert await protocol(Vty.close, key) == 127

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "exit 5")
        await collect(protocol, key)

        assert await protocol(Vty.close, key) == 5
        assert await protocol(Vty.close, key) == 0

    @pytest.mark.asyncio
    async def test_close_ends_a_running_child(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "sleep 300")

        # The child ignores nothing, so the first signal is enough.
        assert await protocol(Vty.close, key) != 0

    @pytest.mark.asyncio
    async def test_two_sessions_run_at_once(self, protocol: Protocol) -> None:
        first = await open_terminal(protocol, "/bin/sh", "-c", "echo FIRST_DONE")
        second = await open_terminal(protocol, "/bin/sh", "-c", "echo SECOND_DONE")

        assert first != second
        assert b"FIRST_DONE" in await collect(protocol, first)
        assert b"SECOND_DONE" in await collect(protocol, second)
        assert await protocol(Vty.close, first) == 0
        assert await protocol(Vty.close, second) == 0


class TestPipeSession:
    @pytest.mark.asyncio
    async def test_bytes_pass_through_unchanged(self, protocol: Protocol) -> None:
        """A pipe adds no carriage return and keeps binary data."""
        key = await protocol(Vty.open, ["/bin/sh", "-c", "printf 'a\\nb\\n'"], want_pty=False)

        assert await collect(protocol, key) == b"a\nb\n"
        assert await protocol(Vty.close, key) == 0

    @pytest.mark.asyncio
    async def test_end_of_input_lets_cat_finish(self, protocol: Protocol) -> None:
        """A pipe has a real half close, so cat sees the end of its input."""
        key = await protocol(Vty.open, ["cat"], want_pty=False)
        await protocol(Vty.write, key, b"piped payload\n")
        await protocol(Vty.end_input, key)

        assert await collect(protocol, key) == b"piped payload\n"
        assert await protocol(Vty.close, key) == 0

    @pytest.mark.asyncio
    async def test_child_has_no_terminal(self, protocol: Protocol) -> None:
        key = await protocol(Vty.open, ["/bin/sh", "-c", "tty || true"], want_pty=False)

        assert b"not a tty" in await collect(protocol, key)
        await protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_missing_command_raises(self, protocol: Protocol) -> None:
        with pytest.raises(OSError):
            await protocol(Vty.open, ["/nonexistent/rmote-vty-binary"], want_pty=False)

    @pytest.mark.asyncio
    async def test_exit_status_is_reported(self, protocol: Protocol) -> None:
        key = await protocol(Vty.open, ["/bin/sh", "-c", "exit 9"], want_pty=False)
        await collect(protocol, key)

        assert await protocol(Vty.close, key) == 9


class TestOtherCallsKeepWorking:
    @pytest.mark.asyncio
    async def test_a_call_progresses_while_output_streams(self, protocol: Protocol) -> None:
        key = await open_terminal(protocol, "/bin/sh", "-c", "dd if=/dev/zero bs=1000 count=400 2>/dev/null")

        seen = 0
        calls = 0
        async with asyncio.timeout(TIMEOUT):
            async for chunk in protocol(Vty.output, key):
                if calls < 3:
                    # The streaming output shares the connection with this call.
                    # The call must not wait for the stream to finish.
                    await protocol(Vty.resize, key, 30, 90)
                    calls += 1
                seen += len(chunk)

        assert calls == 3

        assert seen == 400_000
        await protocol(Vty.close, key)


class TestResourceOwnership:
    """A failure must not leave a descriptor or a process behind.

    These checks call the tool in this process, because the count of open
    descriptors is what they measure.
    """

    @pytest.mark.asyncio
    async def test_output_survives_the_child_closing_its_descriptors(self, tmp_path) -> None:
        marker = tmp_path / "closed"
        script = (
            "import os, pathlib, sys; "
            "os.write(1, b'LAST_OUTPUT\\n'); os.closerange(0, 3); "
            "pathlib.Path(sys.argv[1]).touch(); sys.exit(7)"
        )
        before = open_descriptors()
        key = await Vty.open([sys.executable, "-c", script, str(marker)], launcher=launcher_source())
        try:
            async with asyncio.timeout(TIMEOUT):
                # Do not read until the child has closed every slave copy.
                # On macOS, the last close would discard its unread output.
                while not marker.exists():
                    await asyncio.sleep(0.01)
                output = b"".join([chunk async for chunk in Vty.output(key)])
                assert output == b"LAST_OUTPUT\r\n"
                assert await Vty.wait(key) == 7
        finally:
            await Vty.close(key)
        assert open_descriptors() == before

    @pytest.mark.asyncio
    async def test_close_without_reading_releases_the_terminal(self) -> None:
        before = open_descriptors()
        key = await Vty.open(["/bin/sh", "-c", "printf unread"], launcher=launcher_source())
        await Vty.close(key)
        assert open_descriptors() == before

    @pytest.mark.asyncio
    async def test_a_failed_start_releases_the_terminal(self, tmp_path) -> None:
        missing = str(tmp_path / "missing-directory")
        before = open_descriptors()
        sessions = len(Vty.sessions)

        for _ in range(5):
            with pytest.raises(OSError):
                await Vty.open(["/bin/sh"], cwd=missing, launcher=launcher_source())

        assert open_descriptors() == before
        assert len(Vty.sessions) == sessions

    @pytest.mark.asyncio
    async def test_a_failed_pipe_start_leaves_nothing(self, tmp_path) -> None:
        before = open_descriptors()
        sessions = len(Vty.sessions)

        for _ in range(5):
            with pytest.raises(OSError):
                await Vty.open(["/nonexistent/rmote-vty-binary"], want_pty=False)

        assert open_descriptors() == before
        assert len(Vty.sessions) == sessions

    @pytest.mark.asyncio
    async def test_close_releases_the_terminal(self) -> None:
        before = open_descriptors()
        key = await Vty.open(["/bin/sh", "-c", "exit 0"], launcher=launcher_source())
        async for _ in Vty.output(key):
            pass

        assert await Vty.close(key) == 0
        assert open_descriptors() == before


class TestControllingTerminal:
    @pytest.mark.asyncio
    async def test_a_program_that_does_not_claim_the_terminal(self, protocol: Protocol) -> None:
        """Opening /dev/tty succeeds only with a controlling terminal.

        bash takes the terminal by itself when it can, so a program that does
        not do that is the honest check.
        """
        script = "import os; open('/dev/tty').close(); print('CTTY_OK', os.ttyname(0))"
        key = await open_terminal(protocol, sys.executable, "-c", script)

        output = await collect(protocol, key)
        assert b"CTTY_OK" in output
        assert await protocol(Vty.close, key) == 0


class TestWritesThatCannotFinish:
    @pytest.mark.asyncio
    async def test_write_after_the_child_exited_is_safe(self, protocol: Protocol) -> None:
        key = await protocol(Vty.open, ["/bin/sh", "-c", "exit 0"], want_pty=False)
        await collect(protocol, key)

        # The child is gone, so the write has nowhere to go. It must not raise.
        await protocol(Vty.write, key, b"ignored\n")
        assert await protocol(Vty.close, key) == 0

    @pytest.mark.asyncio
    async def test_other_calls_run_while_a_write_waits(self, protocol: Protocol) -> None:
        """A full pipe must not stop the remote event loop."""
        key = await protocol(Vty.open, ["/bin/sh", "-c", "sleep 30"], want_pty=False)
        try:
            # More than one pipe buffer, and the child never reads.
            writer = asyncio.ensure_future(protocol(Vty.write, key, b"x" * 1_000_000))
            await asyncio.sleep(0.2)
            assert not writer.done()

            # Another call still goes through and comes back.
            await asyncio.wait_for(protocol(Vty.resize, key, 20, 60), TIMEOUT)
            writer.cancel()
        finally:
            assert await protocol(Vty.close, key) != 0


class TestModuleShape:
    """The module keeps the pieces a POSIX host needs."""

    def test_the_tools_package_imports(self) -> None:
        """An import that fails at module level would break every tool."""
        import rmote.tools

        assert rmote.tools.Vty is Vty

    def test_the_host_reports_a_shell(self) -> None:
        from rmote.tools.vty import default_shell

        assert default_shell()

    def test_child_options_start_a_new_session(self) -> None:
        from rmote.tools.vty import child_options

        assert child_options() == {"start_new_session": True}

    def test_the_terminal_pieces_exist(self) -> None:
        from rmote.tools import vty

        assert hasattr(vty, "TerminalSession")
        assert hasattr(vty, "set_winsize")
        assert hasattr(vty, "take_controlling_terminal")

    def test_the_launcher_takes_the_controlling_terminal(self) -> None:
        assert "TIOCSCTTY" in launcher_source()

    def test_the_pipe_backend_has_no_window_size(self) -> None:
        """A session without a terminal accepts a resize and ignores it."""
        from rmote.tools.vty import PipeSession

        assert PipeSession.resize is Session.resize


class TestTerminalSessionInDocker:
    """The same checks against a Linux remote side.

    A controlling terminal behaves differently on Linux and on macOS.
    """

    @pytest.mark.asyncio
    async def test_child_gets_a_terminal(self, docker_protocol: Protocol) -> None:
        key = await open_terminal(docker_protocol, "/bin/sh", "-c", "tty")

        assert b"/dev/pts/" in await collect(docker_protocol, key)
        await docker_protocol(Vty.close, key)

    @pytest.mark.asyncio
    async def test_job_control_is_available(self, docker_protocol: Protocol) -> None:
        """bash reports no job control when it has no controlling terminal."""
        key = await open_terminal(docker_protocol, "/bin/bash", "-i", "-c", "echo FLAGS=$-")

        output = await collect(docker_protocol, key)
        assert b"no job control" not in output
        assert b"cannot set terminal process group" not in output
        assert await docker_protocol(Vty.close, key) == 0

    @pytest.mark.asyncio
    async def test_pipe_session_keeps_bytes(self, docker_protocol: Protocol) -> None:
        key = await docker_protocol(Vty.open, ["/bin/sh", "-c", "printf 'x\\ny\\n'"], want_pty=False)

        assert await collect(docker_protocol, key) == b"x\ny\n"
        assert await docker_protocol(Vty.close, key) == 0
