"""Regression tests for package object state."""

from unittest.mock import AsyncMock

import pytest

from rmote.tools import apt, pacman


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [apt, pacman])
async def test_package_object_state(module, monkeypatch):
    tool: type[apt.Apt] | type[pacman.Pacman]
    if module is apt:
        monkeypatch.setattr(
            apt.Backend,
            "read_status",
            lambda: {
                "demo": {"Status": "install ok installed", "Version": "1"},
            },
        )
        backend_call = AsyncMock(return_value=(0, "", ""))
        monkeypatch.setattr(apt.Backend, "apt_get", backend_call)
        tool = apt.Apt
        remove_flag = "remove"
    else:
        monkeypatch.setattr(pacman.Backend, "query", AsyncMock(return_value=(True, "1")))
        backend_call = AsyncMock(return_value=(0, "", ""))
        monkeypatch.setattr(pacman.Backend, "pacman", backend_call)
        tool = pacman.Pacman
        remove_flag = "-R"
    result = await tool.converge(module.Package("demo", state=module.State.ABSENT))
    assert result[0].changed is True
    assert backend_call.call_args.args[0] == remove_flag


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [apt, pacman])
@pytest.mark.parametrize("converge", [False, True])
async def test_package_latest_state(module, converge, monkeypatch):
    tool: type[apt.Apt] | type[pacman.Pacman]
    if module is apt:
        monkeypatch.setattr(
            apt.Backend,
            "read_status",
            lambda: {"demo": {"Status": "install ok installed", "Version": "1"}},
        )
        backend_call = AsyncMock(side_effect=[(0, "Inst demo [1] (2 repo)", ""), (0, "", "")])
        monkeypatch.setattr(apt.Backend, "apt_get", backend_call)
        tool, install_flag = apt.Apt, "install"
    else:
        monkeypatch.setattr(pacman.Backend, "query", AsyncMock(return_value=(True, "1")))
        backend_call = AsyncMock(side_effect=[(0, "demo 1 -> 2", ""), (0, "", "")])
        monkeypatch.setattr(pacman.Backend, "pacman", backend_call)
        tool, install_flag = pacman.Pacman, "-S"
    package = module.Package("demo", state=module.State.LATEST)
    result = (await tool.converge(package))[0] if converge else await tool.package(package)
    assert result.changed is True
    assert backend_call.call_args.args[0] == install_flag


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [apt, pacman])
async def test_package_explicit_state_overrides_object(module, monkeypatch):
    tool: type[apt.Apt] | type[pacman.Pacman]
    if module is apt:
        monkeypatch.setattr(apt.Backend, "read_status", lambda: {})
        backend_call = AsyncMock(return_value=(0, "", ""))
        monkeypatch.setattr(apt.Backend, "apt_get", backend_call)
        tool, install_flag = apt.Apt, "install"
    else:
        monkeypatch.setattr(pacman.Backend, "query", AsyncMock(return_value=(False, "")))
        backend_call = AsyncMock(return_value=(0, "", ""))
        monkeypatch.setattr(pacman.Backend, "pacman", backend_call)
        tool, install_flag = pacman.Pacman, "-S"
    package = module.Package("demo", state=module.State.ABSENT)
    assert (await tool.package(package)).changed is False
    backend_call.assert_not_called()
    assert (await tool.package(package, module.State.PRESENT)).changed is True
    assert backend_call.call_args.args[0] == install_flag
    assert package.state == module.State.ABSENT
