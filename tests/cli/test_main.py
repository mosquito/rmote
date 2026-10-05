"""Tests of command dispatch and transport argument parsing."""

import subprocess
import sys
from pathlib import Path

import pytest

from rmote.cli import build_parser


class TestParser:
    def test_transport_takes_the_remainder(self) -> None:
        args = build_parser().parse_args(["shell", "ssh", "server"])

        assert args.transport == ["ssh", "server"]
        assert args.command == []

    def test_transport_keeps_its_own_options(self) -> None:
        args = build_parser().parse_args(["shell", "ssh", "-p", "2222", "-i", "key", "host"])

        assert args.transport == ["ssh", "-p", "2222", "-i", "key", "host"]

    def test_client_options_come_before_the_transport(self) -> None:
        args = build_parser().parse_args(["shell", "--term", "vt100", "--python", "python3.13", "ssh", "host"])

        assert (args.term, args.python) == ("vt100", "python3.13")
        assert args.transport == ["ssh", "host"]

    def test_command_option_repeats_for_arguments(self) -> None:
        args = build_parser().parse_args(["shell", "--command", "/bin/bash", "--command=-l", "ssh", "host"])

        assert args.command == ["/bin/bash", "-l"]

    def test_empty_transport_is_allowed(self) -> None:
        assert build_parser().parse_args(["shell"]).transport == []

    @pytest.mark.parametrize("command", [["shell"]], ids=["shell"])
    @pytest.mark.parametrize("separator", [[], ["--"]], ids=["implicit", "explicit"])
    def test_transport_help_belongs_to_transport(self, command: list[str], separator: list[str]) -> None:
        transport = ["transport", "--help", "--python", "other-python"]
        assert build_parser().parse_args(command + separator + transport).transport == separator + transport


@pytest.fixture(
    params=[[sys.executable, "-m", "rmote"], [str(Path(sys.executable).with_name("rmote"))]], ids=["module", "script"]
)
def cli(request: pytest.FixtureRequest) -> list[str]:
    return list(request.param)


def test_shell_dispatch_preserves_bytes_transport_and_exit_status(cli: list[str]) -> None:
    payload = bytes(range(256))
    result = subprocess.run(
        [
            *cli,
            "shell",
            "--python",
            sys.executable,
            "--no-pty",
            "--command",
            "/bin/sh",
            "--command=-c",
            "--command",
            'printf "%s" "$RMOTE_CLI_TEST"; cat; exit 7',
            "--",
            "env",
            "RMOTE_CLI_TEST=transport",
        ],
        input=payload,
        capture_output=True,
        timeout=15,
    )
    assert (result.returncode, result.stdout, result.stderr) == (7, b"transport" + payload, b"")


@pytest.mark.parametrize(
    "command, option",
    [([], b"shell"), (["shell"], b"--no-pty")],
    ids=["root", "shell"],
)
def test_help(cli: list[str], command: list[str], option: bytes) -> None:
    result = subprocess.run([*cli, *command, "--help"], capture_output=True, timeout=10)
    assert result.returncode == 0
    assert option in result.stdout
    assert result.stderr == b""


@pytest.mark.parametrize("command", [[], ["unknown"]], ids=["missing", "unknown"])
def test_invalid_command(cli: list[str], command: list[str]) -> None:
    result = subprocess.run([*cli, *command], capture_output=True, timeout=10)
    assert result.returncode == 2
    assert b"usage:" in result.stderr
