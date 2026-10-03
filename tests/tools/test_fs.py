"""Tests for FileSystem tool."""

import os
import stat
from pathlib import Path

import pytest

from rmote.tools import FileSystem
from rmote.tools.fs import LineInFileMatch, StatResult


class TestFileSystem:
    def test_read_bytes(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.bin"
        content = b"Hello, \x00\xff bytes!"
        test_file.write_bytes(content)
        assert FileSystem.read_bytes(str(test_file)) == content

    def test_read_str(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        content = "Hello, world!\nLine 2"
        test_file.write_text(content)
        assert FileSystem.read_str(str(test_file)) == content

    def test_read_str_unicode(self, tmp_path: Path) -> None:
        test_file = tmp_path / "unicode.txt"
        content = "Hello 世界 🌍"
        test_file.write_text(content, encoding="utf-8")
        assert FileSystem.read_str(str(test_file)) == content

    def test_glob_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "test1.txt").touch()
        (tmp_path / "test2.txt").touch()
        (tmp_path / "test.py").touch()
        (tmp_path / "other.md").touch()
        result = FileSystem.glob(str(tmp_path), "*.txt")
        assert len(result) == 2
        assert all(r.endswith(".txt") for r in result)

    def test_glob_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "dir1").mkdir()
        (tmp_path / "dir1" / "file1.txt").touch()
        (tmp_path / "dir2").mkdir()
        (tmp_path / "dir2" / "file2.txt").touch()
        assert len(FileSystem.glob(str(tmp_path), "**/*.txt")) == 2

    def test_glob_with_path_object(self, tmp_path: Path) -> None:
        (tmp_path / "test.txt").touch()
        result = FileSystem.glob(tmp_path, "*.txt")
        assert len(result) == 1
        assert result[0].endswith("test.txt")

    def test_read_bytes_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            FileSystem.read_bytes("/nonexistent/file.txt")

    def test_read_str_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            FileSystem.read_str("/nonexistent/file.txt")


class TestLineInFile:
    # --- no regexp: ensure-present (append) behavior ---

    def test_appends_line_when_absent(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbar\n")
        diff = FileSystem.line_in_file(str(f), line="baz")
        assert f.read_text() == "foo\nbar\nbaz\n"
        assert diff != ""

    def test_no_change_when_line_already_present(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbaz\nbar\n")
        assert FileSystem.line_in_file(str(f), line="baz") == ""
        assert f.read_text() == "foo\nbaz\nbar\n"

    def test_strip_true_considers_padded_line_present(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("  baz  \n")
        assert FileSystem.line_in_file(str(f), line="baz", strip=True) == ""

    def test_strip_false_treats_padded_line_as_absent(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("  baz  \n")
        diff = FileSystem.line_in_file(str(f), line="baz", strip=False)
        assert diff != ""
        assert "baz\n" in f.read_text()

    # --- regexp: replace behavior ---

    def test_regexp_replaces_matching_line(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbar\nbaz\n")
        diff = FileSystem.line_in_file(str(f), line="qux", regexp=r"bar")
        assert f.read_text() == "foo\nqux\nbaz\n"
        assert "-bar" in diff
        assert "+qux" in diff

    def test_regexp_partial_match(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo=old\nbar\n")
        FileSystem.line_in_file(str(f), line="foo=new", regexp=r"^foo=")
        assert f.read_text() == "foo=new\nbar\n"

    def test_regexp_first_mode_replaces_only_first(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nfoo\nbar\n")
        FileSystem.line_in_file(str(f), line="baz", regexp=r"foo")
        assert f.read_text() == "baz\nfoo\nbar\n"

    def test_regexp_all_mode_replaces_every_match(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbar\nfoo\n")
        diff = FileSystem.line_in_file(str(f), line="baz", regexp=r"foo", match=LineInFileMatch.ALL)
        assert f.read_text() == "baz\nbar\nbaz\n"
        assert diff.count("-foo") == 2
        assert diff.count("+baz") == 2

    def test_regexp_no_match_appends_line(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbar\n")
        diff = FileSystem.line_in_file(str(f), line="baz", regexp=r"nothere")
        assert f.read_text() == "foo\nbar\nbaz\n"
        assert diff != ""

    def test_regexp_idempotent_when_line_already_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbaz\nbar\n")
        assert FileSystem.line_in_file(str(f), line="baz", regexp=r"baz") == ""
        assert f.read_text() == "foo\nbaz\nbar\n"

    # --- file creation ---

    def test_create_true_appends_to_new_file(self, tmp_path: Path) -> None:
        f = tmp_path / "new.txt"
        diff = FileSystem.line_in_file(str(f), line="hello", create=True)
        assert f.read_text() == "hello"
        assert diff != ""

    def test_create_false_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            FileSystem.line_in_file(str(tmp_path / "missing.txt"), line="x")

    # --- newline preservation ---

    def test_preserves_trailing_newline_on_replace(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbar\n")
        FileSystem.line_in_file(str(f), line="baz", regexp=r"bar")
        assert f.read_text() == "foo\nbaz\n"

    def test_preserves_no_trailing_newline_on_replace(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"foo\nbar")
        FileSystem.line_in_file(str(f), line="baz", regexp=r"bar")
        assert f.read_bytes() == b"foo\nbaz"

    def test_preserves_trailing_newline_on_append(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\n")
        FileSystem.line_in_file(str(f), line="baz")
        assert f.read_text() == "foo\nbaz\n"

    def test_preserves_no_trailing_newline_on_append(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"foo")
        FileSystem.line_in_file(str(f), line="baz")
        assert f.read_bytes() == b"foo\nbaz"

    def test_no_match_does_not_modify_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        original = b"foo\nbar"
        f.write_bytes(original)
        FileSystem.line_in_file(str(f), line="foo")  # "foo" already present
        assert f.read_bytes() == original

    # --- diff format ---

    def test_diff_is_unified_format(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("foo\nbar\nbaz\n")
        diff = FileSystem.line_in_file(str(f), line="qux", regexp=r"bar")
        assert diff.startswith("---")
        assert "+++" in diff
        assert "@@" in diff


class TestWrite:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        p = tmp_path / "new.txt"
        assert FileSystem.write(str(p), "hello") is True
        assert p.read_text() == "hello"

    def test_creates_with_bytes(self, tmp_path: Path) -> None:
        p = tmp_path / "data.bin"
        assert FileSystem.write(str(p), b"\x00\xff") is True
        assert p.read_bytes() == b"\x00\xff"

    def test_noop_when_content_and_mode_match(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_bytes(b"same")
        p.chmod(0o644)
        assert FileSystem.write(str(p), b"same", mode=0o644) is False
        assert p.read_bytes() == b"same"

    def test_changed_on_content_diff(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_bytes(b"old")
        p.chmod(0o644)
        assert FileSystem.write(str(p), b"new", mode=0o644) is True
        assert p.read_bytes() == b"new"

    def test_changed_on_mode_diff(self, tmp_path: Path) -> None:
        p = tmp_path / "f.txt"
        p.write_bytes(b"same")
        p.chmod(0o644)
        assert FileSystem.write(str(p), b"same", mode=0o600) is True
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_sets_file_mode(self, tmp_path: Path) -> None:
        p = tmp_path / "secret.txt"
        FileSystem.write(str(p), "x", mode=0o600)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_str_content_encoded_as_utf8(self, tmp_path: Path) -> None:
        p = tmp_path / "utf8.txt"
        FileSystem.write(str(p), "héllo")
        assert p.read_bytes() == "héllo".encode()


class TestDirectory:
    def test_creates_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "newdir"
        assert FileSystem.directory(str(d)) is True
        assert d.is_dir()

    def test_creates_nested_directories(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b" / "c"
        assert FileSystem.directory(str(d)) is True
        assert d.is_dir()

    def test_noop_when_already_exists_with_same_mode(self, tmp_path: Path) -> None:
        d = tmp_path / "existing"
        d.mkdir(mode=0o755)
        assert FileSystem.directory(str(d), mode=0o755) is False

    def test_changed_on_mode_diff(self, tmp_path: Path) -> None:
        d = tmp_path / "existing"
        d.mkdir(mode=0o755)
        assert FileSystem.directory(str(d), mode=0o700) is True
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_sets_directory_mode(self, tmp_path: Path) -> None:
        d = tmp_path / "restricted"
        FileSystem.directory(str(d), mode=0o700)
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


class TestSymlink:
    def test_creates_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("x")
        link = tmp_path / "link"
        assert FileSystem.symlink(str(link), str(target)) is True
        assert link.is_symlink()
        assert os.readlink(link) == str(target)

    def test_noop_when_already_correct(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("x")
        link = tmp_path / "link"
        os.symlink(str(target), link)
        assert FileSystem.symlink(str(link), str(target)) is False

    def test_replaces_wrong_symlink(self, tmp_path: Path) -> None:
        t1 = tmp_path / "t1.txt"
        t2 = tmp_path / "t2.txt"
        t1.write_text("a")
        t2.write_text("b")
        link = tmp_path / "link"
        os.symlink(str(t1), link)
        assert FileSystem.symlink(str(link), str(t2)) is True
        assert os.readlink(link) == str(t2)

    def test_replaces_regular_file(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text("x")
        regular = tmp_path / "regular"
        regular.write_text("y")
        assert FileSystem.symlink(str(regular), str(target)) is True
        assert regular.is_symlink()


class TestAbsent:
    def test_removes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert FileSystem.absent(str(f)) is True
        assert not f.exists()

    def test_removes_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.write_text("x")
        link = tmp_path / "link"
        os.symlink(str(target), link)
        assert FileSystem.absent(str(link)) is True
        assert not link.exists()
        assert target.exists()  # target untouched

    def test_noop_when_already_absent(self, tmp_path: Path) -> None:
        assert FileSystem.absent(str(tmp_path / "ghost")) is False

    def test_removes_empty_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "emptydir"
        d.mkdir()
        assert FileSystem.absent(str(d)) is True
        assert not d.exists()

    def test_raises_on_non_empty_directory_without_recursive(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "file.txt").write_text("x")
        with pytest.raises(OSError):
            FileSystem.absent(str(d), recursive=False)

    def test_removes_non_empty_directory_recursively(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "nested").mkdir()
        (d / "nested" / "file.txt").write_text("x")
        assert FileSystem.absent(str(d), recursive=True) is True
        assert not d.exists()


class TestStat:
    def test_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("hello")
        f.chmod(0o600)
        result = FileSystem.stat(str(f))
        assert result.exists is True
        assert result.is_file is True
        assert result.is_dir is False
        assert result.is_symlink is False
        assert result.size == 5
        assert result.mode == 0o600
        assert result.path == str(f)

    def test_existing_directory(self, tmp_path: Path) -> None:
        result = FileSystem.stat(str(tmp_path))
        assert result.exists is True
        assert result.is_dir is True
        assert result.is_file is False

    def test_symlink_not_followed(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text("x")
        link = tmp_path / "link"
        os.symlink(str(target), link)
        result = FileSystem.stat(str(link))
        assert result.is_symlink is True
        assert result.link_target == str(target)
        assert result.is_file is False

    def test_missing_path_returns_not_exists(self, tmp_path: Path) -> None:
        result = FileSystem.stat(str(tmp_path / "ghost"))
        assert result == StatResult(path=str(tmp_path / "ghost"), exists=False)

    def test_mtime_is_float(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("x")
        result = FileSystem.stat(str(f))
        assert isinstance(result.mtime, float)
        assert result.mtime > 0
