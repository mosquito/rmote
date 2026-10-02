"""Regression tests for package object state."""

from unittest.mock import Mock

import pytest

from rmote.tools import apt, pacman


@pytest.mark.parametrize("module", [apt, pacman])
def test_package_object_state(module, monkeypatch):
    tool: type[apt.Apt] | type[pacman.Pacman]
    if module is apt:
        monkeypatch.setattr(apt.Apt, "_status", None)
        monkeypatch.setattr(
            apt.Backend,
            "read_status",
            lambda: {
                "demo": {"Status": "install ok installed", "Version": "1"},
            },
        )
        backend_call = Mock(return_value=(0, "", ""))
        monkeypatch.setattr(apt.Backend, "apt_get", backend_call)
        tool = apt.Apt
        remove_flag = "remove"
    else:
        monkeypatch.setattr(pacman.Backend, "query", lambda name: (True, "1"))
        backend_call = Mock(return_value=(0, "", ""))
        monkeypatch.setattr(pacman.Backend, "pacman", backend_call)
        tool = pacman.Pacman
        remove_flag = "-R"
    result = tool.converge(module.Package("demo", state=module.State.ABSENT))
    assert result[0].changed is True
    assert backend_call.call_args.args[0] == remove_flag


@pytest.mark.parametrize("module", [apt, pacman])
@pytest.mark.parametrize("converge", [False, True])
def test_package_latest_state(module, converge, monkeypatch):
    tool: type[apt.Apt] | type[pacman.Pacman]
    if module is apt:
        monkeypatch.setattr(apt.Apt, "_status", None)
        monkeypatch.setattr(
            apt.Backend,
            "read_status",
            lambda: {"demo": {"Status": "install ok installed", "Version": "1"}},
        )
        backend_call = Mock(side_effect=[(0, "Inst demo [1] (2 repo)", ""), (0, "", "")])
        monkeypatch.setattr(apt.Backend, "apt_get", backend_call)
        tool, install_flag = apt.Apt, "install"
    else:
        monkeypatch.setattr(pacman.Backend, "query", lambda name: (True, "1"))
        backend_call = Mock(side_effect=[(0, "demo 1 -> 2", ""), (0, "", "")])
        monkeypatch.setattr(pacman.Backend, "pacman", backend_call)
        tool, install_flag = pacman.Pacman, "-S"
    package = module.Package("demo", state=module.State.LATEST)
    result = tool.converge(package)[0] if converge else tool.package(package)
    assert result.changed is True
    assert backend_call.call_args.args[0] == install_flag


@pytest.mark.parametrize("module", [apt, pacman])
def test_package_explicit_state_overrides_object(module, monkeypatch):
    tool: type[apt.Apt] | type[pacman.Pacman]
    if module is apt:
        monkeypatch.setattr(apt.Apt, "_status", None)
        monkeypatch.setattr(apt.Backend, "read_status", lambda: {})
        backend_call = Mock(return_value=(0, "", ""))
        monkeypatch.setattr(apt.Backend, "apt_get", backend_call)
        tool, install_flag = apt.Apt, "install"
    else:
        monkeypatch.setattr(pacman.Backend, "query", lambda name: (False, ""))
        backend_call = Mock(return_value=(0, "", ""))
        monkeypatch.setattr(pacman.Backend, "pacman", backend_call)
        tool, install_flag = pacman.Pacman, "-S"
    package = module.Package("demo", state=module.State.ABSENT)
    assert tool.package(package).changed is False
    backend_call.assert_not_called()
    assert tool.package(package, module.State.PRESENT).changed is True
    assert backend_call.call_args.args[0] == install_flag
    assert package.state == module.State.ABSENT
