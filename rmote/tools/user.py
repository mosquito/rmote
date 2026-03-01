import logging
import os
from dataclasses import dataclass
from pathlib import Path

from rmote.protocol import Tool, process


@dataclass
class Result:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    changed: bool


@dataclass
class GroupResult:
    name: str
    gid: int
    changed: bool


class Backend:
    @staticmethod
    def useradd(*args: str) -> tuple[int, str, str]:
        logging.debug("calling useradd with args: %s", args)
        result = process("useradd", *args, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def usermod(*args: str) -> tuple[int, str, str]:
        logging.debug("calling usermod with args: %s", args)
        result = process("usermod", *args, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def userdel(*args: str) -> tuple[int, str, str]:
        logging.debug("calling userdel with args: %s", args)
        result = process("userdel", *args, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def groupadd(*args: str) -> tuple[int, str, str]:
        logging.debug("calling groupadd with args: %s", args)
        result = process("groupadd", *args, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def groupdel(*args: str) -> tuple[int, str, str]:
        logging.debug("calling groupdel with args: %s", args)
        result = process("groupdel", *args, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def lookup(name: str) -> "tuple[int, int, str, str] | None":
        """Return (uid, gid, home, shell) or None if user does not exist."""
        import pwd

        try:
            pw = pwd.getpwnam(name)
            return pw.pw_uid, pw.pw_gid, pw.pw_dir, pw.pw_shell
        except KeyError:
            return None

    @staticmethod
    def lookup_group(name: str) -> "int | None":
        """Return gid or None if group does not exist."""
        import grp

        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None

    @staticmethod
    def get_groups(name: str) -> list[str]:
        """Return list of supplementary group names for a user."""
        import grp

        return [g.gr_name for g in grp.getgrall() if name in g.gr_mem]


class User(Tool):
    """Manage users, groups, SSH keys, and sudoers on the remote host. Requires root."""

    @staticmethod
    def present(
        name: str,
        *,
        uid: int | None = None,
        gid: int | None = None,
        comment: str = "",
        home: str | None = None,
        shell: str = "/bin/bash",
        groups: list[str] | None = None,
        append_groups: bool = True,
        system: bool = False,
        create_home: bool = True,
    ) -> Result:
        """
        Ensure a user exists with the given attributes. Idempotent.

        Args:
            name: Username
            uid: Numeric UID (optional)
            gid: Primary GID or group name (optional)
            comment: GECOS field
            home: Home directory path (default: /home/<name>)
            shell: Login shell
            groups: Supplementary groups
            append_groups: If True, add to groups without removing existing ones
            system: Create as system user (no home, lower UID range)
            create_home: Create home directory if it does not exist
        """
        existing = Backend.lookup(name)

        if existing is None:
            args: list[str] = []
            if uid is not None:
                args += ["--uid", str(uid)]
            if gid is not None:
                args += ["--gid", str(gid)]
            if comment:
                args += ["--comment", comment]
            if home:
                args += ["--home-dir", home]
            if shell:
                args += ["--shell", shell]
            if groups:
                args += ["--groups", ",".join(groups)]
            if system:
                args.append("--system")
            if create_home and not system:
                args.append("--create-home")
            else:
                args.append("--no-create-home")
            args.append(name)

            rc, _, err = Backend.useradd(*args)
            if rc != 0:
                raise RuntimeError(f"useradd {name!r} failed:\n{err}")

            info = Backend.lookup(name)
            assert info is not None
            uid_out, gid_out, home_out, shell_out = info
            return Result(name=name, uid=uid_out, gid=gid_out, home=home_out, shell=shell_out, changed=True)

        uid_out, gid_out, home_out, shell_out = existing
        mod_args: list[str] = []

        if shell and shell_out != shell:
            mod_args += ["--shell", shell]
        if comment:
            mod_args += ["--comment", comment]
        if home and home_out != home:
            mod_args += ["--home", home, "--move-home"]
        if groups is not None:
            flag = "--append" if append_groups else ""
            if flag:
                mod_args += [flag, "--groups", ",".join(groups)]
            else:
                mod_args += ["--groups", ",".join(groups)]

        if mod_args:
            mod_args.append(name)
            rc, _, err = Backend.usermod(*mod_args)
            if rc != 0:
                raise RuntimeError(f"usermod {name!r} failed:\n{err}")
            info = Backend.lookup(name)
            assert info is not None
            uid_out, gid_out, home_out, shell_out = info
            return Result(name=name, uid=uid_out, gid=gid_out, home=home_out, shell=shell_out, changed=True)

        return Result(name=name, uid=uid_out, gid=gid_out, home=home_out, shell=shell_out, changed=False)

    @staticmethod
    def absent(name: str, *, remove_home: bool = False) -> bool:
        """
        Ensure a user does not exist. Returns True if user was removed.

        Args:
            name: Username
            remove_home: Also remove the user's home directory
        """
        if Backend.lookup(name) is None:
            return False
        args = ["--remove"] if remove_home else []
        args.append(name)
        rc, _, err = Backend.userdel(*args)
        if rc != 0:
            raise RuntimeError(f"userdel {name!r} failed:\n{err}")
        return True

    @staticmethod
    def group_present(name: str, *, gid: int | None = None, system: bool = False) -> GroupResult:
        """Ensure a group exists. Idempotent."""
        existing_gid = Backend.lookup_group(name)
        if existing_gid is not None:
            return GroupResult(name=name, gid=existing_gid, changed=False)

        args: list[str] = []
        if gid is not None:
            args += ["--gid", str(gid)]
        if system:
            args.append("--system")
        args.append(name)

        rc, _, err = Backend.groupadd(*args)
        if rc != 0:
            raise RuntimeError(f"groupadd {name!r} failed:\n{err}")

        gid_out = Backend.lookup_group(name)
        assert gid_out is not None
        return GroupResult(name=name, gid=gid_out, changed=True)

    @staticmethod
    def group_absent(name: str) -> bool:
        """Ensure a group does not exist. Returns True if group was removed."""
        if Backend.lookup_group(name) is None:
            return False
        rc, _, err = Backend.groupdel(name)
        if rc != 0:
            raise RuntimeError(f"groupdel {name!r} failed:\n{err}")
        return True

    @staticmethod
    def authorized_key(name: str, key: str, *, exclusive: bool = False) -> bool:
        """
        Ensure an SSH public key is present in ~/.ssh/authorized_keys.

        Args:
            name: Username
            key: SSH public key string
            exclusive: If True, replace the entire authorized_keys with only this key
        Returns:
            True if file was changed.
        """
        import pwd

        pw = pwd.getpwnam(name)
        ssh_dir = Path(pw.pw_dir) / ".ssh"
        auth_keys = ssh_dir / "authorized_keys"

        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)

        key = key.strip()

        if exclusive:
            new_content = key + "\n"
            if auth_keys.exists() and auth_keys.read_text() == new_content:
                return False
            auth_keys.write_text(new_content)
            auth_keys.chmod(0o600)
            os.chown(auth_keys, pw.pw_uid, pw.pw_gid)
            return True

        existing = auth_keys.read_text() if auth_keys.exists() else ""
        for line in existing.splitlines():
            if line.strip() == key:
                return False

        with auth_keys.open("a") as f:
            f.write(key + "\n")
        auth_keys.chmod(0o600)
        os.chown(auth_keys, pw.pw_uid, pw.pw_gid)
        return True

    @staticmethod
    def sudoer(name: str, *, nopasswd: bool = True, absent: bool = False) -> bool:
        """
        Manage a sudoers drop-in for a user in /etc/sudoers.d/.

        Args:
            name: Username
            nopasswd: Grant passwordless sudo
            absent: Remove the sudoers entry instead
        Returns:
            True if file was changed.
        """
        sudoers_dir = Path("/etc/sudoers.d")
        sudoers_file = sudoers_dir / name

        if absent:
            if not sudoers_file.exists():
                return False
            sudoers_file.unlink()
            return True

        rule = f"{name} ALL=(ALL) {'NOPASSWD: ' if nopasswd else ''}ALL\n"

        if sudoers_file.exists() and sudoers_file.read_text() == rule:
            return False

        sudoers_dir.mkdir(mode=0o750, exist_ok=True)
        sudoers_file.write_text(rule)
        sudoers_file.chmod(0o440)
        return True
