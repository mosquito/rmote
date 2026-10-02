"""Regression tests for line in file idempotency."""

import pytest

from rmote.tools.fs import FileSystem, LineInFileMatch


def test_line_in_file_is_idempotent_when_regexp_misses(tmp_path):
    path = tmp_path / "example.conf"
    path.write_text("old=1\n")
    FileSystem.line_in_file(str(path), line="desired=2", regexp="^missing=")
    assert FileSystem.line_in_file(str(path), line="desired=2", regexp="^missing=") == ""
    assert path.read_text() == "old=1\ndesired=2\n"


@pytest.mark.parametrize("match", [LineInFileMatch.FIRST, LineInFileMatch.ALL])
@pytest.mark.parametrize("strip", [False, True])
def test_regexp_miss_uses_exact_line_comparison(tmp_path, match, strip):
    path = tmp_path / "example.conf"
    path.write_text("  desired=2  \n")
    diff = FileSystem.line_in_file(str(path), line="desired=2", regexp="^missing=", match=match, strip=strip)
    if strip:
        assert diff == ""
        assert path.read_text() == "  desired=2  \n"
    else:
        assert diff
        assert path.read_text() == "  desired=2  \ndesired=2\n"
    assert (
        FileSystem.line_in_file(
            str(path),
            line="desired=2",
            regexp="^missing=",
            match=match,
            strip=strip,
        )
        == ""
    )
