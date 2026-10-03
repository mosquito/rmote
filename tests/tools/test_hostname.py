"""Tests for Hostname tool."""

from pathlib import Path
from unittest.mock import patch

import pytest

from rmote.tools.hostname import Hostname


class TestGet:
    def test_returns_hostname(self, tmp_path: Path) -> None:
        f = tmp_path / "hostname"
        f.write_text("myhost\n")
        with patch("rmote.tools.hostname._PROC_HOSTNAME", f):
            assert Hostname.get() == "myhost"

    def test_strips_trailing_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "hostname"
        f.write_text("myhost\n\n")
        with patch("rmote.tools.hostname._PROC_HOSTNAME", f):
            assert Hostname.get() == "myhost"


class TestSet:
    def test_noop_when_already_set(self, tmp_path: Path) -> None:
        proc = tmp_path / "hostname"
        etc = tmp_path / "etc_hostname"
        proc.write_text("myhost\n")
        with patch("rmote.tools.hostname._PROC_HOSTNAME", proc), patch("rmote.tools.hostname._HOSTNAME_FILE", etc):
            result = Hostname.set("myhost")
        assert result is False
        assert not etc.exists()

    def test_changes_when_different(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc_hostname"
        etc = tmp_path / "etc_hostname"
        proc.write_text("oldhost\n")
        with patch("rmote.tools.hostname._PROC_HOSTNAME", proc), patch("rmote.tools.hostname._HOSTNAME_FILE", etc):
            result = Hostname.set("newhost")
        assert result is True
        assert proc.read_text() == "newhost\n"
        assert etc.read_text() == "newhost\n"

    def test_writes_both_proc_and_etc(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc_hostname"
        etc = tmp_path / "etc_hostname"
        proc.write_text("old\n")
        with patch("rmote.tools.hostname._PROC_HOSTNAME", proc), patch("rmote.tools.hostname._HOSTNAME_FILE", etc):
            Hostname.set("new")
        assert proc.read_text() == "new\n"
        assert etc.read_text() == "new\n"


class TestHostsEntry:
    def test_appends_new_entry(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts"
        f.write_text("127.0.0.1\tlocalhost\n")
        with patch("rmote.tools.hostname._HOSTS_FILE", f):
            result = Hostname.hosts_entry("10.0.0.1", "myhost", "myhost.local")
        assert result is True
        assert "10.0.0.1\tmyhost myhost.local\n" in f.read_text()

    def test_noop_when_entry_identical(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts"
        f.write_text("127.0.0.1\tlocalhost\n10.0.0.1\tmyhost myhost.local\n")
        with patch("rmote.tools.hostname._HOSTS_FILE", f):
            result = Hostname.hosts_entry("10.0.0.1", "myhost", "myhost.local")
        assert result is False

    def test_replaces_entry_with_different_names(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts"
        f.write_text("127.0.0.1\tlocalhost\n10.0.0.1\toldhost\n")
        with patch("rmote.tools.hostname._HOSTS_FILE", f):
            result = Hostname.hosts_entry("10.0.0.1", "newhost")
        assert result is True
        content = f.read_text()
        assert "10.0.0.1\tnewhost\n" in content
        assert "oldhost" not in content

    def test_comment_lines_are_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts"
        f.write_text("# comment\n127.0.0.1\tlocalhost\n")
        with patch("rmote.tools.hostname._HOSTS_FILE", f):
            result = Hostname.hosts_entry("10.0.0.1", "myhost")
        assert result is True
        lines = f.read_text().splitlines()
        assert lines[0] == "# comment"

    def test_raises_on_empty_names(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts"
        f.write_text("")
        with patch("rmote.tools.hostname._HOSTS_FILE", f):
            with pytest.raises(ValueError, match="hostname"):
                Hostname.hosts_entry("10.0.0.1")

    def test_appends_to_file_without_trailing_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts"
        f.write_bytes(b"127.0.0.1\tlocalhost")
        with patch("rmote.tools.hostname._HOSTS_FILE", f):
            Hostname.hosts_entry("10.0.0.1", "myhost")
        content = f.read_text()
        assert content.startswith("127.0.0.1\tlocalhost\n")
        assert "10.0.0.1\tmyhost\n" in content
