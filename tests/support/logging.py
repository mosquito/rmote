"""Capture worker-thread logs and wait for records, never for a delay."""

import logging
import threading


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.threads: list[int] = []
        self.changed = threading.Condition()

    def emit(self, item: logging.LogRecord) -> None:
        with self.changed:
            self.messages.append(item.getMessage())
            self.threads.append(threading.get_ident())
            self.changed.notify_all()

    def wait(self, count: int) -> None:
        with self.changed:
            assert self.changed.wait_for(lambda: len(self.messages) >= count, 10), self.messages
