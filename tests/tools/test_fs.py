"""Tests for FileSystem tool."""

from pathlib import Path

import pytest

from rmote.tools import FileSystem
from rmote.tools.fs import LineInFileMatch


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
