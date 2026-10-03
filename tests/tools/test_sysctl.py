"""Tests for Sysctl tool."""

from pathlib import Path
from unittest.mock import patch

from rmote.tools.sysctl import Backend, Result, Sysctl


class TestBackendReadConf:
    def test_returns_empty_dict_when_file_missing(self, tmp_path: Path) -> None:
        assert Backend.read_conf(tmp_path / "missing.conf") == {}

    def test_parses_key_value_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "sysctl.conf"
        f.write_text("net.ipv4.ip_forward = 1\nvm.swappiness = 10\n")
        assert Backend.read_conf(f) == {"net.ipv4.ip_forward": "1", "vm.swappiness": "10"}

    def test_ignores_comment_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "sysctl.conf"
        f.write_text("# comment\nnet.ipv4.ip_forward = 1\n")
        assert Backend.read_conf(f) == {"net.ipv4.ip_forward": "1"}

    def test_ignores_blank_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "sysctl.conf"
        f.write_text("\nnet.ipv4.ip_forward = 1\n\n")
        assert Backend.read_conf(f) == {"net.ipv4.ip_forward": "1"}


class TestBackendWriteConf:
    def test_writes_sorted_key_value_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "sysctl.conf"
        Backend.write_conf(f, {"vm.swappiness": "10", "net.ipv4.ip_forward": "1"})
        assert f.read_text() == "net.ipv4.ip_forward = 1\nvm.swappiness = 10\n"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        f = tmp_path / "sysctl.d" / "99-rmote.conf"
        Backend.write_conf(f, {"net.ipv4.ip_forward": "1"})
        assert f.exists()


class TestSysctlGet:
    def test_returns_runtime_value(self) -> None:
        with patch.object(Backend, "get_runtime", return_value="1") as mock:
            assert Sysctl.get("net.ipv4.ip_forward") == "1"
        mock.assert_called_once_with("net.ipv4.ip_forward")


class TestSysctlPresent:
    def _conf(self, tmp_path: Path) -> Path:
        return tmp_path / "99-rmote.conf"

    def test_noop_when_runtime_and_conf_match(self, tmp_path: Path) -> None:
        conf = self._conf(tmp_path)
        Backend.write_conf(conf, {"net.ipv4.ip_forward": "1"})
        with (
            patch.object(Backend, "get_runtime", return_value="1"),
            patch.object(Backend, "apply") as mock_apply,
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            result = Sysctl.present("net.ipv4.ip_forward", "1")
        assert result == Result(key="net.ipv4.ip_forward", value="1", changed=False)
        mock_apply.assert_not_called()

    def test_applies_and_persists_when_runtime_differs(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", return_value="0"),
            patch.object(Backend, "apply") as mock_apply,
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            result = Sysctl.present("net.ipv4.ip_forward", "1")
        assert result.changed is True
        mock_apply.assert_called_once_with("net.ipv4.ip_forward", "1")
        assert Backend.read_conf(tmp_path / "99-rmote.conf") == {"net.ipv4.ip_forward": "1"}

    def test_only_updates_conf_when_runtime_already_correct(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", return_value="1"),
            patch.object(Backend, "apply") as mock_apply,
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            result = Sysctl.present("net.ipv4.ip_forward", "1")
        assert result.changed is True
        mock_apply.assert_not_called()
        assert Backend.read_conf(tmp_path / "99-rmote.conf") == {"net.ipv4.ip_forward": "1"}

    def test_custom_name(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", return_value="0"),
            patch.object(Backend, "apply"),
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            Sysctl.present("net.ipv4.ip_forward", "1", name="50-custom.conf")
        assert (tmp_path / "50-custom.conf").exists()
        assert not (tmp_path / "99-rmote.conf").exists()

    def test_handles_missing_proc_path(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", side_effect=FileNotFoundError),
            patch.object(Backend, "apply") as mock_apply,
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            result = Sysctl.present("net.ipv4.ip_forward", "1")
        assert result.changed is True
        mock_apply.assert_not_called()


class TestSysctlAbsent:
    def test_noop_when_key_not_in_conf(self, tmp_path: Path) -> None:
        with patch.object(Sysctl, "CONF_DIR", tmp_path):
            assert Sysctl.absent("net.ipv4.ip_forward") is False

    def test_removes_key_from_conf(self, tmp_path: Path) -> None:
        conf = tmp_path / "99-rmote.conf"
        Backend.write_conf(conf, {"net.ipv4.ip_forward": "1", "vm.swappiness": "10"})
        with patch.object(Sysctl, "CONF_DIR", tmp_path):
            assert Sysctl.absent("net.ipv4.ip_forward") is True
        remaining = Backend.read_conf(conf)
        assert "net.ipv4.ip_forward" not in remaining
        assert remaining["vm.swappiness"] == "10"

    def test_custom_name(self, tmp_path: Path) -> None:
        conf = tmp_path / "50-custom.conf"
        Backend.write_conf(conf, {"net.ipv4.ip_forward": "1"})
        with patch.object(Sysctl, "CONF_DIR", tmp_path):
            assert Sysctl.absent("net.ipv4.ip_forward", name="50-custom.conf") is True
        assert Backend.read_conf(conf) == {}


class TestSysctlConverge:
    def test_applies_multiple_keys(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", return_value="0"),
            patch.object(Backend, "apply") as mock_apply,
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            results = Sysctl.converge({"net.ipv4.ip_forward": "1", "vm.swappiness": "10"})
        assert len(results) == 2
        assert all(r.changed for r in results)
        assert mock_apply.call_count == 2

    def test_writes_conf_only_once(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", return_value="0"),
            patch.object(Backend, "apply"),
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            Sysctl.converge({"net.ipv4.ip_forward": "1", "vm.swappiness": "10", "fs.file-max": "100000"})
        conf = Backend.read_conf(tmp_path / "99-rmote.conf")
        assert len(conf) == 3

    def test_does_not_write_conf_when_nothing_changed(self, tmp_path: Path) -> None:
        conf = tmp_path / "99-rmote.conf"
        Backend.write_conf(conf, {"net.ipv4.ip_forward": "1"})
        mtime_before = conf.stat().st_mtime
        with (
            patch.object(Backend, "get_runtime", return_value="1"),
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            results = Sysctl.converge({"net.ipv4.ip_forward": "1"})
        assert not results[0].changed
        assert conf.stat().st_mtime == mtime_before

    def test_custom_name(self, tmp_path: Path) -> None:
        with (
            patch.object(Backend, "get_runtime", return_value="0"),
            patch.object(Backend, "apply"),
            patch.object(Sysctl, "CONF_DIR", tmp_path),
        ):
            Sysctl.converge({"net.ipv4.ip_forward": "1"}, name="50-custom.conf")
        assert (tmp_path / "50-custom.conf").exists()
        assert not (tmp_path / "99-rmote.conf").exists()

    def test_subclass_overrides_conf_dir(self, tmp_path: Path) -> None:
        class MySysctl(Sysctl):
            CONF_DIR = tmp_path / "custom_sysctl.d"

        with (
            patch.object(Backend, "get_runtime", return_value="0"),
            patch.object(Backend, "apply"),
        ):
            MySysctl.converge({"net.ipv4.ip_forward": "1"})
        assert (tmp_path / "custom_sysctl.d" / "99-rmote.conf").exists()
