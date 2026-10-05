"""Regression tests for user id convergence."""

from unittest.mock import AsyncMock, Mock

import pytest

from rmote.tools import user


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value", "flag"), [("uid", 2000, "--uid"), ("gid", 2000, "--gid")])
async def test_existing_user_ids_converge(monkeypatch, field, value, flag):
    current = (1000, 1000, "/home/demo", "/bin/bash")
    updated = (2000, 1000, *current[2:]) if field == "uid" else (1000, 2000, *current[2:])
    monkeypatch.setattr(user.Backend, "lookup", Mock(side_effect=[current, updated, updated]))
    usermod = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    result = await user.User.present("demo", **{field: value})
    assert result.changed is True
    assert flag in usermod.call_args.args
    assert getattr(result, field) == value
    assert (await user.User.present("demo", **{field: value})).changed is False
    usermod.assert_called_once_with(flag, str(value), "demo")


@pytest.mark.asyncio
async def test_existing_user_matching_ids_do_not_modify(monkeypatch):
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))
    usermod = AsyncMock()
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    assert (await user.User.present("demo", uid=1000, gid=1000)).changed is False
    usermod.assert_not_called()


@pytest.mark.asyncio
async def test_existing_user_id_change_failure(monkeypatch):
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))
    monkeypatch.setattr(user.Backend, "usermod", AsyncMock(return_value=(1, "", "uid already in use")))
    with pytest.raises(RuntimeError, match="uid already in use"):
        await user.User.present("demo", uid=2000)
