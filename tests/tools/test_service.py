"""Unit tests for Service tool - mocks systemctl via Backend."""

from unittest.mock import patch

import pytest

from rmote.tools.service import Backend, Result, Service


class TestServiceStatus:
    def test_running_and_enabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
        ):
            result = Service.status("nginx")
        assert result == Result(name="nginx", started=True, enabled=True, changed=False)

    def test_stopped_and_disabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
        ):
            result = Service.status("nginx")
        assert result == Result(name="nginx", started=False, enabled=False, changed=False)

    def test_running_but_disabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=False),
        ):
            result = Service.status("sshd")
        assert result.started is True
        assert result.enabled is False
        assert result.changed is False


class TestServiceStart:
    def test_noop_when_already_active(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "start") as mock_start,
        ):
            result = Service.start("nginx")
        mock_start.assert_not_called()
        assert result.changed is False
        assert result.started is True

    def test_starts_when_stopped(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "start") as mock_start,
        ):
            result = Service.start("nginx")
        mock_start.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.started is True


class TestServiceStop:
    def test_noop_when_already_stopped(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "stop") as mock_stop,
        ):
            result = Service.stop("nginx")
        mock_stop.assert_not_called()
        assert result.changed is False
        assert result.started is False

    def test_stops_when_running(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "stop") as mock_stop,
        ):
            result = Service.stop("nginx")
        mock_stop.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.started is False


class TestServiceRestart:
    def test_always_restarts(self) -> None:
        with patch.object(Backend, "is_enabled", return_value=True), patch.object(Backend, "restart") as mock_restart:
            result = Service.restart("nginx")
        mock_restart.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.started is True


class TestServiceReload:
    def test_always_reloads(self) -> None:
        with patch.object(Backend, "is_enabled", return_value=True), patch.object(Backend, "reload") as mock_reload:
            result = Service.reload("nginx")
        mock_reload.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.started is True


class TestServiceEnable:
    def test_noop_when_already_enabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.enable("nginx")
        mock_enable.assert_not_called()
        assert result.changed is False
        assert result.enabled is True

    def test_enables_when_disabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.enable("nginx")
        mock_enable.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.enabled is True


class TestServiceDisable:
    def test_noop_when_already_disabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "disable") as mock_disable,
        ):
            result = Service.disable("nginx")
        mock_disable.assert_not_called()
        assert result.changed is False
        assert result.enabled is False

    def test_disables_when_enabled(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "disable") as mock_disable,
        ):
            result = Service.disable("nginx")
        mock_disable.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.enabled is False


class TestServiceConverge:
    def test_noop_when_desired_state_already_satisfied(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "start") as mock_start,
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.converge("nginx", started=True, enabled=True)
        mock_start.assert_not_called()
        mock_enable.assert_not_called()
        assert result.changed is False

    def test_starts_and_enables_when_both_missing(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "start") as mock_start,
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.converge("nginx", started=True, enabled=True)
        mock_start.assert_called_once_with("nginx")
        mock_enable.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.started is True
        assert result.enabled is True

    def test_stops_and_disables(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "stop") as mock_stop,
            patch.object(Backend, "disable") as mock_disable,
        ):
            result = Service.converge("nginx", started=False, enabled=False)
        mock_stop.assert_called_once_with("nginx")
        mock_disable.assert_called_once_with("nginx")
        assert result.changed is True
        assert result.started is False
        assert result.enabled is False

    def test_starts_without_enabling(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "start") as mock_start,
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.converge("nginx", started=True, enabled=False)
        mock_start.assert_called_once_with("nginx")
        mock_enable.assert_not_called()
        assert result.changed is True

    def test_enables_without_starting(self) -> None:
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "start") as mock_start,
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.converge("nginx", started=False, enabled=True)
        mock_start.assert_not_called()
        mock_enable.assert_called_once_with("nginx")
        assert result.changed is True

    def test_only_start_needed(self) -> None:
        """Service is enabled but stopped - converge(started=True, enabled=True) only starts."""
        with (
            patch.object(Backend, "is_active", return_value=False),
            patch.object(Backend, "is_enabled", return_value=True),
            patch.object(Backend, "start") as mock_start,
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.converge("nginx", started=True, enabled=True)
        mock_start.assert_called_once_with("nginx")
        mock_enable.assert_not_called()
        assert result.changed is True

    def test_only_enable_needed(self) -> None:
        """Service is running but disabled - converge(started=True, enabled=True) only enables."""
        with (
            patch.object(Backend, "is_active", return_value=True),
            patch.object(Backend, "is_enabled", return_value=False),
            patch.object(Backend, "start") as mock_start,
            patch.object(Backend, "enable") as mock_enable,
        ):
            result = Service.converge("nginx", started=True, enabled=True)
        mock_start.assert_not_called()
        mock_enable.assert_called_once_with("nginx")
        assert result.changed is True


class TestBackendError:
    def test_start_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "Unit not found.")):
            with pytest.raises(RuntimeError, match="systemctl start"):
                Backend.start("nonexistent.service")

    def test_stop_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "error")):
            with pytest.raises(RuntimeError, match="systemctl stop"):
                Backend.stop("nonexistent.service")

    def test_enable_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "error")):
            with pytest.raises(RuntimeError, match="systemctl enable"):
                Backend.enable("nonexistent.service")

    def test_restart_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "error")):
            with pytest.raises(RuntimeError, match="systemctl restart"):
                Backend.restart("nonexistent.service")

    def test_reload_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "error")):
            with pytest.raises(RuntimeError, match="systemctl reload"):
                Backend.reload("nonexistent.service")

    def test_disable_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "error")):
            with pytest.raises(RuntimeError, match="systemctl disable"):
                Backend.disable("nonexistent.service")

    def test_daemon_reload_success(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(0, "", "")):
            Backend.daemon_reload()  # must not raise

    def test_daemon_reload_failure_raises(self) -> None:
        with patch.object(Backend, "systemctl", return_value=(1, "", "error")):
            with pytest.raises(RuntimeError, match="systemctl daemon-reload"):
                Backend.daemon_reload()
