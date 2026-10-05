import pytest

from rmote.process import process


class TestProcessFunction:
    def test_process_is_sync_subprocess_helper(self) -> None:
        import inspect

        assert not inspect.iscoroutinefunction(process)
        result = process("echo", "hello", capture_output=True, text=True)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_basic_no_capture(self) -> None:
        """capture_output=False (default) sets stdout/stderr to DEVNULL."""
        result = process("true", capture_output=False)
        assert result.returncode == 0

    def test_capture_output(self) -> None:
        result = process("echo", "hello", capture_output=True, text=True)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_stdin_bytes(self) -> None:
        result = process("cat", stdin=b"hello", capture_output=True)
        assert result.stdout == b"hello"

    def test_stdin_str_is_encoded(self) -> None:
        """str stdin is converted to bytes before passing to subprocess."""
        result = process("cat", stdin="world", capture_output=True)
        assert result.stdout == b"world"

    def test_check_raises(self) -> None:
        import subprocess

        with pytest.raises(subprocess.CalledProcessError):
            process("false", check=True)

    def test_cwd(self, tmp_path) -> None:
        result = process("pwd", capture_output=True, text=True, cwd=str(tmp_path))
        assert str(tmp_path) in result.stdout

    def test_env(self) -> None:
        result = process(
            "sh",
            "-c",
            "echo $RMOTE_TEST_VAR",
            env={"RMOTE_TEST_VAR": "sentinel", "PATH": "/bin:/usr/bin"},
            capture_output=True,
            text=True,
        )
        assert "sentinel" in result.stdout
