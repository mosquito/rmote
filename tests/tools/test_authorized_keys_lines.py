"""Regression tests for authorized keys lines."""

import os
from types import SimpleNamespace

import pytest

from rmote.tools import user


@pytest.mark.parametrize("ending", ["", "\n"])
def test_authorized_key_after_unterminated_line(tmp_path, monkeypatch, ending):
    import pwd

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    keys = ssh_dir / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAAold old" + ending)
    pw = SimpleNamespace(pw_dir=str(tmp_path), pw_uid=1000, pw_gid=1000)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: pw)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    assert user.User.authorized_key("demo", "ssh-ed25519 AAAAnew new") is True
    assert keys.read_text().splitlines() == ["ssh-ed25519 AAAAold old", "ssh-ed25519 AAAAnew new"]
    assert user.User.authorized_key("demo", "ssh-ed25519 AAAAnew new") is False
    assert keys.read_text() == "ssh-ed25519 AAAAold old\nssh-ed25519 AAAAnew new\n"


@pytest.mark.parametrize("exists", [False, True])
def test_authorized_key_empty_file(tmp_path, monkeypatch, exists):
    import pwd

    keys = tmp_path / ".ssh" / "authorized_keys"
    if exists:
        keys.parent.mkdir()
        keys.touch()
    pw = SimpleNamespace(pw_dir=str(tmp_path), pw_uid=1000, pw_gid=1000)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: pw)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    assert user.User.authorized_key("demo", "ssh-ed25519 AAAAnew new") is True
    assert keys.read_text() == "ssh-ed25519 AAAAnew new\n"
