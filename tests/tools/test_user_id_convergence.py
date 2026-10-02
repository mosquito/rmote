"""Regression tests for user id convergence."""

from unittest.mock import Mock

import pytest

from rmote.tools import user


@pytest.mark.parametrize(("field", "value", "flag"), [("uid", 2000, "--uid"), ("gid", 2000, "--gid")])
def test_existing_user_ids_converge(monkeypatch, field, value, flag):
    current = (1000, 1000, "/home/demo", "/bin/bash")
    updated = (2000, 1000, *current[2:]) if field == "uid" else (1000, 2000, *current[2:])
    monkeypatch.setattr(user.Backend, "lookup", Mock(side_effect=[current, updated, updated]))
    usermod = Mock(return_value=(0, "", ""))
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    result = user.User.present("demo", **{field: value})
    assert result.changed is True
    assert flag in usermod.call_args.args
    assert getattr(result, field) == value
    assert user.User.present("demo", **{field: value}).changed is False
    usermod.assert_called_once_with(flag, str(value), "demo")


def test_existing_user_matching_ids_do_not_modify(monkeypatch):
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))
    usermod = Mock()
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    assert user.User.present("demo", uid=1000, gid=1000).changed is False
    usermod.assert_not_called()


def test_existing_user_id_change_failure(monkeypatch):
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))
    monkeypatch.setattr(user.Backend, "usermod", lambda *args: (1, "", "uid already in use"))
    with pytest.raises(RuntimeError, match="uid already in use"):
        user.User.present("demo", uid=2000)
