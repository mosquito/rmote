"""Regression tests for user idempotency."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rmote.tools import user


@pytest.mark.asyncio
async def test_existing_user_groups_are_idempotent(monkeypatch):
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))
    monkeypatch.setattr(user.Backend, "get_groups", lambda name: ["developers"])
    usermod = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    result = await user.User.present("demo", groups=["developers"])
    assert result.changed is False
    usermod.assert_not_called()


@pytest.mark.asyncio
async def test_existing_user_comment_is_idempotent(monkeypatch):
    import pwd

    account = SimpleNamespace(pw_gecos="", pw_uid=1000, pw_gid=1000, pw_dir="/home/demo", pw_shell="/bin/bash")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))

    def update_comment(*args):
        account.pw_gecos = args[args.index("--comment") + 1]
        return 0, "", ""

    usermod = AsyncMock(side_effect=update_comment)
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    assert (await user.User.present("demo", comment="Demo account")).changed is True
    assert (await user.User.present("demo", comment="Demo account")).changed is False
    assert usermod.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "desired", "append", "changed"),
    [
        (["developers", "ops"], ["developers"], True, False),
        (["developers", "ops"], ["developers"], False, True),
        (["developers", "ops"], ["ops", "developers"], False, False),
        (["developers"], [], True, False),
        (["developers"], [], False, True),
        ([], [], False, False),
        ([], ["developers"], True, True),
        ([], ["developers"], False, True),
    ],
)
async def test_existing_user_group_sets(monkeypatch, current, desired, append, changed):
    monkeypatch.setattr(user.Backend, "lookup", lambda name: (1000, 1000, "/home/demo", "/bin/bash"))
    monkeypatch.setattr(user.Backend, "get_groups", lambda name: current)
    usermod = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(user.Backend, "usermod", usermod)
    result = await user.User.present("demo", groups=desired, append_groups=append)
    assert result.changed is changed
    if changed:
        prefix = ["--append"] if append else []
        usermod.assert_called_once_with(*prefix, "--groups", ",".join(desired), "demo")
    else:
        usermod.assert_not_called()
