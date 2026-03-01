import logging
from typing import Any

from rmote.protocol import Tool


class Logger(Tool):
    """Control logging on the remote side and forward records to the local process.

    Log records emitted on the remote side are forwarded over the protocol channel
    and appear locally under the ``rmote.remote.<name>`` logger hierarchy.
    """

    @staticmethod
    def _check_level(level: str) -> int:
        level_upper = level.upper()
        if level_upper not in ["NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            raise ValueError(f"Invalid log level: {level}")
        return int(getattr(logging, level_upper))

    @classmethod
    def set_log_level(cls, level: str) -> None:
        """Set the root logger level on the remote side.

        Args:
            level: One of ``"NOTSET"``, ``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
                ``"ERROR"``, or ``"CRITICAL"`` (case-insensitive).

        Raises:
            ValueError: If *level* is not a recognised level name.
        """
        levelno = cls._check_level(level)
        logging.info("Setting log level to %s", level)
        logging.getLogger().setLevel(levelno)

    @classmethod
    def log(cls, level: str, message: str, *args: Any) -> None:
        """Emit a log record on the remote side.

        The record is forwarded to the local logging system via the protocol's
        LOG packet and appears under the ``rmote.remote.<name>`` logger.

        Args:
            level: Log level name (e.g. ``"INFO"``, ``"DEBUG"``).
            message: Log message format string (``%``-style).
            *args: Arguments merged into *message*.

        Raises:
            ValueError: If *level* is not a recognised level name.
        """
        levelno = Logger._check_level(level)
        logging.log(levelno, message, *args)
