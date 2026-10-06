import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from rmote.cli import build_parser


def run_cli(*args: str, asyncio_debug: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # Normal progress output must not depend on slow-callback diagnostics
    # inherited from CI. Exercise debug logging separately below.
    env.pop("PYTHONASYNCIODEBUG", None)
    if asyncio_debug:
        env["PYTHONASYNCIODEBUG"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "rmote", "rsync", *args],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def test_directory_round_trip_logs_reuse_and_delete(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source, remote, local = (root / name for name in ("source", "remote", "copy"))
    source.mkdir()
    (source / "empty").mkdir()
    (source / "hello.txt").write_text("hello")
    (source / "link").symlink_to("hello.txt")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("excluded")
    upload = run_cli("--exclude", ".git/", "--", str(source), f"remote:{remote}")
    assert upload.returncode == 0, upload.stderr
    assert upload.stdout == ""
    assert "file 'hello.txt': 5 bytes transferred, 0 reused" in upload.stderr
    assert "mkdir 'empty'" in upload.stderr
    assert "symlink 'link' -> 'hello.txt'" in upload.stderr
    assert "1 files checked, 5 bytes transferred" in upload.stderr
    assert (
        len([line for line in upload.stderr.splitlines() if "hello.txt" in line and not line.startswith("symlink")])
        == 1
    )
    assert (remote / "hello.txt").read_text() == "hello"
    assert (remote / "empty").is_dir()
    assert (remote / "link").readlink() == Path("hello.txt")
    assert not (remote / ".git").exists()
    again = run_cli("--exclude", ".git/", "--", str(source), f"remote:{remote}")
    assert again.returncode == 0, again.stderr
    assert "Up to date: 1 files checked, 0 bytes transferred, 5 bytes reused" in again.stderr
    assert not any(line.startswith("file ") for line in again.stderr.splitlines())
    assert len(again.stderr.splitlines()) == 1

    (remote / "stale").write_text("remove me")
    (remote / ".git").mkdir()
    (remote / ".git" / "keep").write_text("protected")
    cleanup = run_cli("--exclude", ".git/", "-d", str(source), f"remote:{remote}")
    assert cleanup.returncode == 0, cleanup.stderr
    assert "delete 'stale' (1 entries)" in cleanup.stderr
    assert not (remote / "stale").exists()
    assert (remote / ".git" / "keep").read_text() == "protected"

    download = run_cli("--exclude", ".git/", "--", f"remote:{remote}", str(local))
    assert download.returncode == 0, download.stderr
    assert (local / "hello.txt").read_bytes() == b"hello"
    assert (local / "link").is_symlink()
    assert (local / "empty").is_dir()
    assert not (local / ".git").exists()


def test_transport_quoting_interpreter_and_dash_r(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    wrapper, record = root / "transport with spaces.py", root / "argv.json"
    wrapper.write_text(
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n"
    )
    source, target = root / "source", root / "target"
    source.mkdir()
    (source / "file").write_text("payload")
    command = shlex.join([sys.executable, str(wrapper), "one argument with spaces"])
    result = run_cli("-r", command, "--python", sys.executable, str(source), f"remote:{target}")
    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text()) == ["one argument with spaces", sys.executable, "-qui"]
    assert (target / "file").read_text() == "payload"


def test_multiple_excludes_stop_at_next_option() -> None:
    args = build_parser().parse_args(
        [
            "rsync",
            "--exclude",
            ".venv",
            ".idea",
            ".cache",
            "*.pyc",
            "--delete",
            "-r",
            "docker exec -i elated_bouman",
            ".",
            "remote:/opt/rmote2",
        ]
    )
    assert args.exclude == [".venv", ".idea", ".cache", "*.pyc"]
    assert (args.source, args.target) == (".", "remote:/opt/rmote2")
    assert args.transport == ["docker", "exec", "-i", "elated_bouman"]
    assert args.delete is True


def test_multiple_and_repeated_excludes_apply_together(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    source.mkdir()
    for name in (".venv", ".idea", ".cache"):
        (source / name).mkdir()
        (source / name / "skip").touch()
    (source / "skip.pyc").touch()
    (source / "keep.py").write_text("keep")
    result = run_cli("-x", ".venv", ".idea", ".cache", "--exclude", "*.pyc", "--", str(source), f"remote:{target}")
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in target.iterdir()) == ["keep.py"]


@pytest.mark.parametrize("uploading", [False, True], ids=["download", "upload"])
def test_delete_excluded_logs_previously_copied_files(tmp_path: Path, uploading: bool) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    source.mkdir()
    (source / "keep.py").write_text("keep")
    (source / "skip.pyc").write_text("cached")
    (source / ".cache").mkdir()
    (source / ".cache" / "data").write_text("cached")
    args = (str(source), f"remote:{target}") if uploading else (f"remote:{source}", str(target))
    first = run_cli(*args)
    assert first.returncode == 0, first.stderr
    (target / "extra").write_text("extra")
    cleanup = run_cli("-x", "*.pyc", ".cache/", "-D" if uploading else "--delete-excluded", *args)
    assert cleanup.returncode == 0, cleanup.stderr
    assert sorted(path.name for path in target.iterdir()) == ["keep.py"]
    assert "delete 'skip.pyc' (1 entries)" in cleanup.stderr
    assert "delete '.cache' (2 entries)" in cleanup.stderr
    assert "delete 'extra' (1 entries)" in cleanup.stderr
    assert "4 entries deleted" in cleanup.stderr
    assert not any(line.startswith("file ") for line in cleanup.stderr.splitlines())
    assert (source / "skip.pyc").read_text() == "cached"


@pytest.mark.parametrize("uploading", [False, True], ids=["download", "upload"])
@pytest.mark.parametrize("destination_directory", [False, True], ids=["file", "directory"])
def test_single_file_and_permissions(tmp_path: Path, uploading: bool, destination_directory: bool) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    payload = os.urandom(13000)
    source.write_bytes(payload)
    source.chmod(0o750)
    if destination_directory:
        target.mkdir()
    args = (str(source), f"remote:{target}") if uploading else (f"remote:{source}", str(target))
    result = run_cli("--block-size", "4096", *args)
    assert result.returncode == 0, result.stderr
    copied = target / source.name if destination_directory else target
    assert copied.read_bytes() == payload
    assert copied.stat().st_mode & 0o777 == 0o750
    again = run_cli(*args)
    assert again.returncode == 0, again.stderr
    assert "Up to date: 1 files checked, 0 bytes transferred, 13000 bytes reused" in again.stderr
    assert not any(line.startswith("file ") for line in again.stderr.splitlines())
    assert len(again.stderr.splitlines()) == 1


def test_quiet_and_keep_permissions(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    source.write_bytes(b"new")
    source.chmod(0o750)
    target.write_bytes(b"old")
    target.chmod(0o600)
    result = run_cli("--quiet", "--no-preserve-mode", str(source), f"remote:{target}")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("debug", [False, True])
def test_asyncio_diagnostics_follow_debug_flag(tmp_path: Path, debug: bool) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")
    options = ["--debug"] if debug else []
    result = run_cli(*options, str(source), f"remote:{target}", asyncio_debug=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert ("execute program" in result.stderr) is debug
    assert "Close running child process" not in result.stderr
    assert "7 bytes transferred" in result.stderr
    assert target.read_bytes() == b"payload"


def test_partial_reuse_still_logs_changed_file(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    source.write_bytes(b"a" * 4096 + b"b" * 4096 + b"c" * 4096)
    target.write_bytes(b"a" * 4096 + b"x" * 4096 + b"c" * 4096)
    result = run_cli("--block-size", "4096", str(source), f"remote:{target}")
    assert result.returncode == 0, result.stderr
    files = [line for line in result.stderr.splitlines() if line.startswith("file ")]
    assert len(files) == 1
    assert "4096 bytes transferred, 8192 reused" in files[0]
    assert target.read_bytes() == source.read_bytes()


def test_new_empty_file_is_logged(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    source.touch()
    result = run_cli(str(source), f"remote:{target}")
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("file ") for line in result.stderr.splitlines())
    assert target.read_bytes() == b""


@pytest.mark.parametrize(
    "args",
    [
        ("source", "target"),
        ("remote:source", "remote:target"),
        ("source", "remote:"),
        ("--concurrency", "0", "source", "remote:target"),
        ("--block-size", "0", "source", "remote:target"),
        ("--block-size", "16777217", "source", "remote:target"),
        ("--exec", "", "source", "remote:target"),
        ("--exec", "'unfinished", "source", "remote:target"),
    ],
)
def test_invalid_arguments_before_transport(args: tuple[str, ...]) -> None:
    result = run_cli("--exec", "/does-not-exist", *args)
    assert result.returncode == 2, result.stderr
    assert "usage:" in result.stderr
    assert "Traceback" not in result.stderr


def test_failed_transport_includes_reason(tmp_path: Path) -> None:
    result = run_cli("--exec", "sh -c 'echo transport-failed >&2; exit 3'", str(tmp_path), "remote:/unused")
    assert result.returncode == 1
    assert "transport-failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert "files checked" not in result.stderr


def test_failure_preserves_destination_and_does_not_report_success(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    source, target = root / "source", root / "target"
    source.mkdir()
    (source / "conflict").write_text("file")
    target.mkdir()
    (target / "conflict").mkdir()
    (target / "stale").write_text("keep on failure")
    result = run_cli("--delete", str(source), f"remote:{target}")
    assert result.returncode == 1
    assert "Type conflict" in result.stderr
    assert (target / "stale").read_text() == "keep on failure"
    assert "files checked" not in result.stderr
