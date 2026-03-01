from typing import Any

from rmote.protocol import Tool


class UserLookup(Tool):
    @staticmethod
    def user(name: str) -> "dict[str, Any] | None":
        import pwd

        try:
            pw = pwd.getpwnam(name)
            return {"uid": pw.pw_uid, "gid": pw.pw_gid, "shell": pw.pw_shell, "home": pw.pw_dir}
        except KeyError:
            return None

    @staticmethod
    def group(name: str) -> "dict[str, Any] | None":
        import grp

        try:
            g = grp.getgrnam(name)
            return {"gid": g.gr_gid, "members": list(g.gr_mem)}
        except KeyError:
            return None

    @staticmethod
    def supplementary_groups(username: str) -> "list[str]":
        import grp

        return [g.gr_name for g in grp.getgrall() if username in g.gr_mem]
