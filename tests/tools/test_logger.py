"""Tests for Logger tool."""

import logging

import pytest

from rmote.tools import Logger


class TestLogger:
    def test_set_log_level_notset(self) -> None:
        Logger.set_log_level("NOTSET")
        assert logging.getLogger().level == logging.NOTSET

    def test_set_log_level_debug(self) -> None:
        Logger.set_log_level("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_set_log_level_info(self) -> None:
        Logger.set_log_level("INFO")
        assert logging.getLogger().level == logging.INFO

    def test_set_log_level_warning(self) -> None:
        Logger.set_log_level("WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_set_log_level_error(self) -> None:
        Logger.set_log_level("ERROR")
        assert logging.getLogger().level == logging.ERROR

    def test_set_log_level_critical(self) -> None:
        Logger.set_log_level("CRITICAL")
        assert logging.getLogger().level == logging.CRITICAL

    def test_set_log_level_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid log level"):
            Logger.set_log_level("INVALID")

        with pytest.raises(ValueError, match="Invalid log level"):
            Logger.set_log_level("TRACE")


class TestLoggerLog:
    def test_log_info(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.INFO):
            Logger.log("INFO", "hello %s", "world")
        assert "hello world" in caplog.text

    def test_log_debug(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.DEBUG):
            Logger.log("DEBUG", "debug message")
        assert "debug message" in caplog.text

    def test_log_warning(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            Logger.log("WARNING", "warn msg")
        assert "warn msg" in caplog.text

    def test_log_invalid_level(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="Invalid log level"):
            Logger.log("TRACE", "msg")
