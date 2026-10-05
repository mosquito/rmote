"""Tests for the local terminal client and the rmote-shell entry point."""

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from rmote.shell import EscapeFilter, NonBlocking, TerminalMode, build_parser, regular_file, terminal_size

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX terminal")

DRIVE_TIMEOUT = 60


def drive_shell(
    args: list[str],
    script: bytes,
    size: tuple[int, int] = (24, 80),
    env: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Run rmote-shell behind a terminal, feed *script*, collect the output.

    The client needs a real terminal to enter raw mode, so the test owns the
    master side and the client gets the slave side as its standard descriptors.
    The script is sent after the first output, because the client forwards input
    only once the session is open.
    """
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))

    child_env = dict(os.environ)
    child_env.setdefault("SHELL", "/bin/sh")
    child_env["TERM"] = "xterm"
    if env:
        child_env.update(env)

    process = subprocess.Popen(
        [sys.executable, "-m", "rmote.shell", *args],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=child_env,
    )
    os.close(slave_fd)

    out = bytearray()
    pending = script
    deadline = time.monotonic() + DRIVE_TIMEOUT
    send_deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if pending:
                    os.write(master_fd, pending)
                    pending = b""
                continue
            if pending and time.monotonic() > send_deadline:
                os.write(master_fd, pending)
                pending = b""
                continue
            if process.poll() is not None:
                break
        returncode = process.wait(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master_fd)
    return returncode, bytes(out)


def run_piped(args: list[str], payload: bytes) -> subprocess.CompletedProcess[bytes]:
    """Run rmote-shell with pipes instead of a terminal."""
    return subprocess.run(
        [sys.executable, "-m", "rmote.shell", *args],
        input=payload,
        capture_output=True,
        timeout=DRIVE_TIMEOUT,
    )


def run_from_file(args: list[str], source) -> subprocess.CompletedProcess[bytes]:
    """Run rmote-shell with its input redirected from a regular file."""
    with open(source, "rb") as handle:
        return subprocess.run(
            [sys.executable, "-m", "rmote.shell", *args],
            stdin=handle,
            capture_output=True,
            timeout=DRIVE_TIMEOUT,
        )


class TestEscapeFilter:
    def test_forwards_plain_input(self) -> None:
        assert EscapeFilter(b"~").feed(b"ls -la\n") == (b"ls -la\n", False)

    def test_closes_at_line_start(self) -> None:
        assert EscapeFilter(b"~").feed(b"~.") == (b"", True)

    def test_closes_after_a_line_end(self) -> None:
        assert EscapeFilter(b"~").feed(b"echo hi\n~.") == (b"echo hi\n", True)

    def test_doubled_escape_sends_one_character(self) -> None:
        assert EscapeFilter(b"~").feed(b"~~") == (b"~", False)

    def test_escape_inside_a_line_stays_literal(self) -> None:
        assert EscapeFilter(b"~").feed(b"cd ~/src\n") == (b"cd ~/src\n", False)

    def test_unknown_character_after_escape_passes_both(self) -> None:
        assert EscapeFilter(b"~").feed(b"~x") == (b"~x", False)

    def test_state_survives_between_chunks(self) -> None:
        shell_filter = EscapeFilter(b"~")

        assert shell_filter.feed(b"~") == (b"", False)
        assert shell_filter.feed(b".") == (b"", True)

    def test_empty_escape_disables_the_filter(self) -> None:
        assert EscapeFilter(b"").feed(b"~.") == (b"~.", False)


class TestHelpers:
    def test_terminal_size_falls_back_without_a_terminal(self, tmp_path) -> None:
        target = tmp_path / "plain.txt"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT)
        try:
            assert terminal_size(fd) == (24, 80)
        finally:
            os.close(fd)

    def test_terminal_size_reads_a_terminal(self) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 43, 117, 0, 0))
            assert terminal_size(slave_fd) == (43, 117)
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_regular_file_is_detected(self, tmp_path) -> None:
        target = tmp_path / "plain.txt"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT)
        master_fd, slave_fd = pty.openpty()
        try:
            assert regular_file(fd)
            assert not regular_file(slave_fd)
        finally:
            os.close(fd)
            os.close(master_fd)
            os.close(slave_fd)

    def test_terminal_mode_ignores_a_plain_file(self, tmp_path) -> None:
        target = tmp_path / "plain.txt"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT)
        try:
            mode = TerminalMode(fd)
            mode.enter()
            assert not mode.active
            mode.restore()
        finally:
            os.close(fd)

    def test_terminal_mode_restores_a_terminal(self) -> None:
        """Raw mode clears echo and line editing, and the restore brings them back.

        The whole attribute block is not comparable. The kernel also reports
        volatile status bits there, such as PENDIN on macOS.
        """
        master_fd, slave_fd = pty.openpty()
        interactive = termios.ECHO | termios.ICANON
        lflag = 3
        try:
            assert termios.tcgetattr(slave_fd)[lflag] & interactive == interactive

            mode = TerminalMode(slave_fd)
            mode.enter()
            assert mode.active
            assert termios.tcgetattr(slave_fd)[lflag] & interactive == 0

            mode.restore()
            assert not mode.active
            assert termios.tcgetattr(slave_fd)[lflag] & interactive == interactive
            # A second restore must not fail.
            mode.restore()
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_non_blocking_restores_the_flags(self) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            before = fcntl.fcntl(slave_fd, fcntl.F_GETFL)
            flags = NonBlocking(slave_fd)
            flags.enter()
            assert fcntl.fcntl(slave_fd, fcntl.F_GETFL) & os.O_NONBLOCK

            flags.restore()
            assert fcntl.fcntl(slave_fd, fcntl.F_GETFL) == before
            # A second restore must not fail.
            flags.restore()
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_non_blocking_skips_a_regular_file(self, tmp_path) -> None:
        target = tmp_path / "plain.txt"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT)
        try:
            flags = NonBlocking(fd)
            flags.enter()
            assert not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
            flags.restore()
        finally:
            os.close(fd)


class TestParser:
    def test_transport_takes_the_remainder(self) -> None:
        args = build_parser().parse_args(["ssh", "server"])

        assert args.transport == ["ssh", "server"]
        assert args.command == []

    def test_transport_keeps_its_own_options(self) -> None:
        args = build_parser().parse_args(["ssh", "-p", "2222", "-i", "key", "host"])

        assert args.transport == ["ssh", "-p", "2222", "-i", "key", "host"]

    def test_client_options_come_before_the_transport(self) -> None:
        args = build_parser().parse_args(["--term", "vt100", "--python", "python3.13", "ssh", "host"])

        assert (args.term, args.python) == ("vt100", "python3.13")
        assert args.transport == ["ssh", "host"]

    def test_command_option_repeats_for_arguments(self) -> None:
        args = build_parser().parse_args(["--command", "/bin/bash", "--command=-l", "ssh", "host"])

        assert args.command == ["/bin/bash", "-l"]

    def test_empty_transport_is_allowed(self) -> None:
        assert build_parser().parse_args([]).transport == []


class TestTerminalSessionEndToEnd:
    """The client runs as a separate process behind a real terminal."""

    def test_runs_a_command_and_returns_its_status(self) -> None:
        status, output = drive_shell(
            ["--python", sys.executable, "--command", "/bin/sh"],
            b"echo SHELL_MARKER_OK\nexit 3\n",
        )

        assert b"SHELL_MARKER_OK" in output
        assert status == 3

    def test_default_command_is_the_login_shell(self) -> None:
        status, _ = drive_shell(
            ["--python", sys.executable],
            b"exit 5\n",
            env={"SHELL": "/bin/sh"},
        )

        assert status == 5

    def test_local_window_size_reaches_the_remote_terminal(self) -> None:
        status, output = drive_shell(
            ["--python", sys.executable, "--command", "/bin/sh"],
            b"stty size\nexit 0\n",
            size=(37, 99),
        )

        assert b"37 99" in output
        assert status == 0

    def test_escape_sequence_closes_the_session(self) -> None:
        status, output = drive_shell(
            ["--python", sys.executable, "--command", "/bin/sh"],
            b"\n~.",
        )

        assert b"session closed" in output
        assert status != 0

    def test_escape_can_be_disabled(self) -> None:
        status, output = drive_shell(
            ["--python", sys.executable, "--command", "/bin/sh", "--escape", ""],
            b"echo TILDE_TEST~.\nexit 0\n",
        )

        assert b"session closed" not in output
        assert status == 0

    def test_transport_command_wraps_the_interpreter(self) -> None:
        status, output = drive_shell(
            ["--python", sys.executable, "--command", "/bin/sh", "--", "env", "-u", "RMOTE_UNSET_ME"],
            b"echo VIA_TRANSPORT\nexit 0\n",
        )

        assert b"VIA_TRANSPORT" in output
        assert status == 0


class TestPipedSessionEndToEnd:
    """A redirected input gives pipes, so the bytes stay exactly as they are."""

    def test_piped_input_reaches_the_command_and_ends(self) -> None:
        result = run_piped(["--python", sys.executable, "--command", "cat"], b"piped payload\n")

        assert result.stdout == b"piped payload\n"
        assert result.returncode == 0

    def test_piped_output_has_no_carriage_return(self) -> None:
        result = run_piped(
            [
                "--python",
                sys.executable,
                "--command",
                "/bin/sh",
                "--command=-c",
                "--command",
                "printf 'a\\nb\\n'",
            ],
            b"",
        )

        assert result.stdout == b"a\nb\n"
        assert result.returncode == 0

    def test_binary_payload_passes_through(self) -> None:
        payload = bytes(range(256)) * 20
        result = run_piped(["--python", sys.executable, "--command", "cat"], payload)

        assert result.stdout == payload
        assert result.returncode == 0

    def test_input_from_a_regular_file(self, tmp_path) -> None:
        """A selector cannot watch a regular file, so this path is its own.

        The guide shows this form, and a wait on the descriptor would fail with
        EINVAL instead of reading the file.
        """
        source = tmp_path / "input.txt"
        source.write_bytes(b"from a regular file\n")

        result = run_from_file(["--python", sys.executable, "--command", "cat"], source)

        assert result.stdout == b"from a regular file\n"
        assert result.returncode == 0

    def test_binary_file_redirect_passes_through(self, tmp_path) -> None:
        source = tmp_path / "input.bin"
        payload = bytes(range(256)) * 40
        source.write_bytes(payload)

        result = run_from_file(["--python", sys.executable, "--command", "cat"], source)

        assert result.stdout == payload
        assert result.returncode == 0

    def test_exit_status_of_a_piped_command(self) -> None:
        result = run_piped(
            ["--python", sys.executable, "--command", "/bin/sh", "--command=-c", "--command", "exit 7"],
            b"",
        )

        assert result.returncode == 7
