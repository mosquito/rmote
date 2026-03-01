"""Tests for User tool.

Unit tests: sudoer() and authorized_key() using tmp_path (no root needed).
Integration tests: user/group lifecycle via docker_protocol (runs as root in container),
verified via pwd/grp through the UserLookup helper tool.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rmote.protocol import Protocol
from rmote.tools.exec import Exec
from rmote.tools.fs import FileSystem
from rmote.tools.user import User
from tests.tools_cases.user_lookup import UserLookup

pytestmark = pytest.mark.timeout(60)


# ---------------------------------------------------------------------------
# Unit tests - no Docker, no root
# ---------------------------------------------------------------------------


class TestSudoer:
    def test_creates_sudoers_file(self, tmp_path: Path) -> None:
        sudoers_d = tmp_path / "sudoers.d"
        sudoers_d.mkdir()
        sudoers_file = sudoers_d / "alice"

        with patch("rmote.tools.user.Path", side_effect=lambda p: Path(p.replace("/etc/sudoers.d", str(sudoers_d)))):
            changed = User.sudoer("alice")

        assert changed is True
        assert sudoers_file.read_text() == "alice ALL=(ALL) NOPASSWD: ALL\n"
        assert oct(sudoers_file.stat().st_mode)[-3:] == "440"

    def test_idempotent_when_file_already_correct(self, tmp_path: Path) -> None:
        sudoers_d = tmp_path / "sudoers.d"
        sudoers_d.mkdir()
        sudoers_file = sudoers_d / "alice"
        sudoers_file.write_text("alice ALL=(ALL) NOPASSWD: ALL\n")
        sudoers_file.chmod(0o440)

        with patch("rmote.tools.user.Path", side_effect=lambda p: Path(p.replace("/etc/sudoers.d", str(sudoers_d)))):
            changed = User.sudoer("alice")

        assert changed is False

    def test_with_password_prompt(self, tmp_path: Path) -> None:
        sudoers_d = tmp_path / "sudoers.d"
        sudoers_d.mkdir()
        sudoers_file = sudoers_d / "bob"

        with patch("rmote.tools.user.Path", side_effect=lambda p: Path(p.replace("/etc/sudoers.d", str(sudoers_d)))):
            User.sudoer("bob", nopasswd=False)

        assert sudoers_file.read_text() == "bob ALL=(ALL) ALL\n"

    def test_absent_removes_file(self, tmp_path: Path) -> None:
        sudoers_d = tmp_path / "sudoers.d"
        sudoers_d.mkdir()
        sudoers_file = sudoers_d / "alice"
        sudoers_file.write_text("alice ALL=(ALL) NOPASSWD: ALL\n")

        with patch("rmote.tools.user.Path", side_effect=lambda p: Path(p.replace("/etc/sudoers.d", str(sudoers_d)))):
            changed = User.sudoer("alice", absent=True)

        assert changed is True
        assert not sudoers_file.exists()

    def test_absent_noop_when_missing(self, tmp_path: Path) -> None:
        sudoers_d = tmp_path / "sudoers.d"
        sudoers_d.mkdir()

        with patch("rmote.tools.user.Path", side_effect=lambda p: Path(p.replace("/etc/sudoers.d", str(sudoers_d)))):
            changed = User.sudoer("ghost", absent=True)

        assert changed is False


class TestAuthorizedKey:
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@host"
    KEY2 = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQ other@host"

    def _make_pw(self, home: str) -> MagicMock:
        pw = MagicMock()
        pw.pw_dir = home
        pw.pw_uid = os.getuid()
        pw.pw_gid = os.getgid()
        return pw

    def test_appends_key_to_new_file(self, tmp_path: Path) -> None:
        home = str(tmp_path)
        with patch("pwd.getpwnam", return_value=self._make_pw(home)), patch("os.chown"):
            changed = User.authorized_key("alice", self.KEY)

        assert changed is True
        auth_keys = tmp_path / ".ssh" / "authorized_keys"
        assert auth_keys.read_text().strip() == self.KEY

    def test_idempotent_when_key_already_present(self, tmp_path: Path) -> None:
        home = str(tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        auth_keys = ssh_dir / "authorized_keys"
        auth_keys.write_text(self.KEY + "\n")

        with patch("pwd.getpwnam", return_value=self._make_pw(home)), patch("os.chown"):
            changed = User.authorized_key("alice", self.KEY)

        assert changed is False
        assert auth_keys.read_text().strip() == self.KEY

    def test_appends_second_key(self, tmp_path: Path) -> None:
        home = str(tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        auth_keys = ssh_dir / "authorized_keys"
        auth_keys.write_text(self.KEY + "\n")

        with patch("pwd.getpwnam", return_value=self._make_pw(home)), patch("os.chown"):
            changed = User.authorized_key("alice", self.KEY2)

        assert changed is True
        lines = [ln for ln in auth_keys.read_text().splitlines() if ln.strip()]
        assert self.KEY in lines
        assert self.KEY2 in lines

    def test_exclusive_replaces_all_keys(self, tmp_path: Path) -> None:
        home = str(tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        auth_keys = ssh_dir / "authorized_keys"
        auth_keys.write_text(self.KEY + "\n" + self.KEY2 + "\n")

        with patch("pwd.getpwnam", return_value=self._make_pw(home)), patch("os.chown"):
            changed = User.authorized_key("alice", self.KEY, exclusive=True)

        assert changed is True
        assert auth_keys.read_text() == self.KEY + "\n"

    def test_exclusive_noop_when_already_sole_key(self, tmp_path: Path) -> None:
        home = str(tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        auth_keys = ssh_dir / "authorized_keys"
        auth_keys.write_text(self.KEY + "\n")

        with patch("pwd.getpwnam", return_value=self._make_pw(home)), patch("os.chown"):
            changed = User.authorized_key("alice", self.KEY, exclusive=True)

        assert changed is False


# ---------------------------------------------------------------------------
# Integration tests - require docker (run as root inside container)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_present_creates_user(docker_protocol: Protocol) -> None:
    result = await docker_protocol(User.present, "testuser")
    assert result.changed is True

    info = await docker_protocol(UserLookup.user, "testuser")
    assert info is not None
    assert info["uid"] == result.uid
    assert info["gid"] == result.gid
    assert info["shell"] == result.shell


@pytest.mark.asyncio
async def test_user_present_idempotent(docker_protocol: Protocol) -> None:
    await docker_protocol(User.present, "testuser")
    result = await docker_protocol(User.present, "testuser")
    assert result.changed is False

    info = await docker_protocol(UserLookup.user, "testuser")
    assert info is not None
    assert info["uid"] == result.uid


@pytest.mark.asyncio
async def test_user_present_with_shell(docker_protocol: Protocol) -> None:
    result = await docker_protocol(User.present, "shelluser", shell="/bin/sh")
    assert result.changed is True

    info = await docker_protocol(UserLookup.user, "shelluser")
    assert info is not None
    assert info["shell"] == "/bin/sh"


@pytest.mark.asyncio
async def test_user_present_system_user(docker_protocol: Protocol) -> None:
    result = await docker_protocol(User.present, "sysuser", system=True)
    assert result.changed is True

    info = await docker_protocol(UserLookup.user, "sysuser")
    assert info is not None
    assert info["uid"] < 1000
    assert info["uid"] == result.uid


@pytest.mark.asyncio
async def test_user_absent_removes_user(docker_protocol: Protocol) -> None:
    await docker_protocol(User.present, "tempuser")
    assert await docker_protocol(UserLookup.user, "tempuser") is not None

    removed = await docker_protocol(User.absent, "tempuser")
    assert removed is True

    assert await docker_protocol(UserLookup.user, "tempuser") is None


@pytest.mark.asyncio
async def test_user_absent_idempotent(docker_protocol: Protocol) -> None:
    assert await docker_protocol(UserLookup.user, "neverexisted") is None

    removed = await docker_protocol(User.absent, "neverexisted")
    assert removed is False


@pytest.mark.asyncio
async def test_group_present_creates_group(docker_protocol: Protocol) -> None:
    result = await docker_protocol(User.group_present, "testgroup")
    assert result.changed is True

    info = await docker_protocol(UserLookup.group, "testgroup")
    assert info is not None
    assert info["gid"] == result.gid


@pytest.mark.asyncio
async def test_group_present_idempotent(docker_protocol: Protocol) -> None:
    first = await docker_protocol(User.group_present, "testgroup")
    second = await docker_protocol(User.group_present, "testgroup")
    assert second.changed is False

    info = await docker_protocol(UserLookup.group, "testgroup")
    assert info is not None
    assert info["gid"] == first.gid


@pytest.mark.asyncio
async def test_group_absent_removes_group(docker_protocol: Protocol) -> None:
    await docker_protocol(User.group_present, "tempgroup")
    assert await docker_protocol(UserLookup.group, "tempgroup") is not None

    removed = await docker_protocol(User.group_absent, "tempgroup")
    assert removed is True

    assert await docker_protocol(UserLookup.group, "tempgroup") is None


@pytest.mark.asyncio
async def test_group_absent_idempotent(docker_protocol: Protocol) -> None:
    assert await docker_protocol(UserLookup.group, "neverexisted") is None

    removed = await docker_protocol(User.group_absent, "neverexisted")
    assert removed is False


@pytest.mark.asyncio
async def test_user_with_supplementary_group(docker_protocol: Protocol) -> None:
    await docker_protocol(User.group_present, "editors")
    await docker_protocol(User.present, "writer", groups=["editors"])

    groups = await docker_protocol(UserLookup.supplementary_groups, "writer")
    assert "editors" in groups


@pytest.mark.asyncio
async def test_authorized_key_via_docker(docker_protocol: Protocol) -> None:
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@rmote"
    info = await docker_protocol(User.present, "keyuser", create_home=True)

    changed = await docker_protocol(User.authorized_key, "keyuser", key)
    assert changed is True

    auth_keys = await docker_protocol(FileSystem.read_str, f"{info.home}/.ssh/authorized_keys")
    assert key in auth_keys

    # idempotent
    changed = await docker_protocol(User.authorized_key, "keyuser", key)
    assert changed is False


@pytest.mark.asyncio
async def test_sudoer_via_docker(docker_protocol: Protocol) -> None:
    await docker_protocol(Exec.shell, "apt-get install -y sudo 2>/dev/null || true", check=False)

    changed = await docker_protocol(User.sudoer, "deployuser")
    assert changed is True

    content = await docker_protocol(FileSystem.read_str, "/etc/sudoers.d/deployuser")
    assert "deployuser" in content
    assert "NOPASSWD" in content

    # idempotent
    changed = await docker_protocol(User.sudoer, "deployuser")
    assert changed is False

    # remove
    changed = await docker_protocol(User.sudoer, "deployuser", absent=True)
    assert changed is True

    rc = await docker_protocol(Exec.shell, "test -f /etc/sudoers.d/deployuser", check=False)
    assert rc.returncode != 0
