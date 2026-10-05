import fcntl
import os
import pty
import select
import subprocess
import sys
import termios
import time
from collections.abc import Iterator

import pytest


class Session:
    def __init__(self, asynchronous: bool) -> None:
        self.asynchronous = asynchronous
        self.master, slave = pty.openpty()
        args = [sys.executable, "-m", "rmote", "repl"]
        if asynchronous:
            args.append("--async")
        self.process = subprocess.Popen(
            args,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "TERM": "xterm"},
            start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(slave, termios.TIOCSCTTY, 0),
        )
        os.close(slave)
        self.output = bytearray()
        self.commands = 0

    def until(self, marker: bytes) -> bytes:
        deadline = time.monotonic() + 10
        while marker not in self.output:
            assert time.monotonic() < deadline, bytes(self.output)
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                chunk = os.read(self.master, 65536)
                assert chunk, bytes(self.output)
                self.output.extend(chunk)
        end = self.output.index(marker) + len(marker)
        result = bytes(self.output[:end])
        del self.output[:end]
        return result

    def send(self, text: str) -> bytes:
        self.commands += 1
        marker = f"RMOTE_TEST_DONE_{self.commands}"
        os.write(self.master, f"{text}\nprint({marker!r})\n".encode())
        # readline can redraw an extra prompt on SIGINT. A result marker
        # synchronizes with executed input rather than a stale redraw.
        return self.until(f"\r\n{marker}\r\n".encode()) + self.until(b">>> ")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        os.close(self.master)

    def wait(self) -> int:
        """Drain terminal output while the child restores its terminal state."""
        deadline = time.monotonic() + 10
        while self.process.poll() is None:
            assert time.monotonic() < deadline, bytes(self.output)
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                self.output.extend(chunk)
                if not chunk:
                    break
        return self.process.wait(timeout=5)


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def session(request: pytest.FixtureRequest) -> Iterator[Session]:
    console = Session(request.param)
    try:
        # The banner also has a >>> example. Wait for its final line and then
        # the actual prompt before feeding commands.
        console.until(b'"memory"]')
        console.until(b">>> ")
        yield console
    finally:
        console.close()
