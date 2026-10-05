"""Regression tests for process text input."""

import sys

import pytest

from rmote.process import process


def test_process_text_input():
    result = process(
        sys.executable,
        "-c",
        "import sys; print(sys.stdin.read(), end='')",
        stdin="hello",
        capture_output=True,
        text=True,
    )
    assert result.stdout == "hello"


@pytest.mark.parametrize("stdin", ["hello", b"hello"])
def test_process_binary_input(stdin):
    result = process(
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        stdin=stdin,
        capture_output=True,
    )
    assert result.stdout == b"hello"


def test_process_text_rejects_bytes():
    with pytest.raises(TypeError, match="stdin must be str when text=True"):
        process(sys.executable, "-c", "pass", stdin=b"hello", text=True)
