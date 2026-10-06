import contextlib
import os
import pty
import select
import signal
import subprocess
import termios
import time

from rmote.tools.vty import set_winsize
from tests.cli.sshmux.conftest import Server


class Terminal:
    def __init__(self, server: Server, rows: int, cols: int) -> None:
        self.master, self.slave = pty.openpty()
        set_winsize(self.master, rows, cols)
        self.original = termios.tcgetattr(self.slave)
        self.process = subprocess.Popen(
            server.command("-tt", "host", "/bin/sh"),
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env=dict(os.environ, TERM="xterm-256color"),
            start_new_session=True,
        )

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def until(self, marker: bytes) -> bytes:
        output = bytearray()
        deadline = time.monotonic() + 10
        while marker not in output:
            assert time.monotonic() < deadline, bytes(output)
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                output.extend(os.read(self.master, 65536))
        return bytes(output)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        os.close(self.master)
        os.close(self.slave)

    def wait(self) -> int:
        # On macOS ssh restores the tty with TCSADRAIN: the terminal emulator
        # must consume the final output before that syscall can return.
        deadline = time.monotonic() + 10
        while self.process.poll() is None:
            assert time.monotonic() < deadline, "ssh did not exit"
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                os.read(self.master, 65536)
        return self.process.returncode

    def assert_restored(self) -> None:
        restored = termios.tcgetattr(self.slave)
        # PENDIN is a transient kernel flag after tcsetattr on macOS.
        restored[3] &= ~termios.PENDIN
        original = list(self.original)
        original[3] &= ~termios.PENDIN
        assert restored == original


def test_two_terminals_resize_independently_and_restore_raw_mode(server: Server):
    with contextlib.ExitStack() as cleanup:
        first, second = Terminal(server, 37, 99), Terminal(server, 28, 85)
        cleanup.callback(first.close)
        cleanup.callback(second.close)
        for terminal, size in ((first, b"37 99"), (second, b"28 85")):
            terminal.until(b"# " if os.getuid() == 0 else b"$ ")
            terminal.send(b"stty size; printf '%s\\n' \"$TERM\"\n")
            output = terminal.until(b"xterm-256color\r\n")
            assert size in output
            assert not termios.tcgetattr(terminal.slave)[3] & termios.ICANON
        set_winsize(first.master, 51, 117)
        os.kill(first.process.pid, signal.SIGWINCH)
        # Wait for a remote resize by querying it, without assuming RPC latency.
        deadline = time.monotonic() + 10
        while True:
            first.send(b"stty size; printf 'END\\n'\n")
            output = first.until(b"END\r\n")
            if b"51 117" in output:
                break
            assert time.monotonic() < deadline
        second.send(b"stty size; printf 'END\\n'\n")
        assert b"28 85" in second.until(b"END\r\n")
        first.send(b"exit 9\n")
        assert first.wait() == 9
        first.assert_restored()
        # The other session remains usable after the first has exited.
        second.send(b"printf 'STILL_HERE\\n'\n")
        second.until(b"STILL_HERE\r\n")
        second.send(b"exit\n")
        assert second.wait() == 0


def test_ctrl_c_reaches_remote_foreground_and_escape_closes_only_session(server: Server):
    terminal = Terminal(server, 24, 80)
    try:
        prompt = b"# " if os.getuid() == 0 else b"$ "
        terminal.until(prompt)
        terminal.send(b"sh -c 'echo RUNNING; exec sleep 300'\n")
        terminal.until(b"RUNNING\r\n")
        terminal.send(b"\x03")
        terminal.until(prompt)
        terminal.send(b"printf 'INTERRUPTED\\n'\n")
        terminal.until(b"INTERRUPTED\r\n")
        terminal.send(b"\r~.")
        assert terminal.wait() == 255
        terminal.assert_restored()
        assert server.run("host", "printf alive").stdout == b"alive"
    finally:
        terminal.close()
