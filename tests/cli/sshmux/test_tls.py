"""TLS selection and real OpenSSH sessions through the encrypted transport."""

import argparse
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from rmote.cli import build_parser, main
from rmote.cli.certs import default_directory
from rmote.cli.sshmux import tls_config
from tests.cli.sshmux import test_daemon
from tests.cli.sshmux.conftest import Server
from tests.cli.sshmux.test_daemon import Daemon

CERTS = Path(__file__).parents[2] / "fixtures" / "tls"
daemon = test_daemon.daemon


def stored_credentials() -> Path:
    directory = default_directory()
    directory.mkdir(parents=True)
    for name in ("controller.crt", "controller.key"):
        shutil.copyfile(CERTS / name, directory / name)
    return directory


def arguments(*options: str) -> argparse.Namespace:
    return build_parser().parse_args(["sshmux", "-S", "/tmp/unused.sock", *options])


@pytest.mark.parametrize("files", [(), ("controller.crt",), ("controller.key",), ("controller-ca.crt",)])
def test_no_complete_defaults_means_no_tls(files):
    directory = default_directory()
    directory.mkdir(parents=True)
    for name in files:
        (directory / name).write_text("unused incomplete credentials")
    assert tls_config(arguments()) is None


def test_stored_files_are_argparse_defaults_and_appear_in_help(capsys):
    directory = stored_credentials()
    args = arguments()
    assert args.tls_cert == args.tls_ca == directory / "controller.crt"
    assert args.tls_key == directory / "controller.key"
    assert args.tls_hostname == socket.gethostname()
    config = tls_config(args)
    assert config is not None
    assert config.certfile == config.cafile == str(directory / "controller.crt")
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["sshmux", "--help"])
    assert error.value.code == 0
    # argparse can wrap long temporary paths across lines.
    help_text = "".join(capsys.readouterr().out.split())
    assert str(directory / "controller.crt") in help_text
    assert str(directory / "controller.key") in help_text


def test_stored_ca_takes_precedence():
    directory = stored_credentials()
    ca = directory / "controller-ca.crt"
    shutil.copyfile(CERTS / "untrusted.crt", ca)
    args = arguments()
    assert args.tls_ca == ca
    config = tls_config(args)
    assert config is not None and config.cafile == str(ca)


def test_explicit_certificate_overrides_self_signed_ca_default():
    stored_credentials()
    config = tls_config(
        arguments("--tls-cert", str(CERTS / "untrusted.crt"), "--tls-key", str(CERTS / "untrusted.key"))
    )
    assert config is not None
    assert config.certfile == config.cafile == str(CERTS / "untrusted.crt")


@pytest.mark.parametrize("option", ["--tls-ca", "--tls-cert", "--tls-key"])
@pytest.mark.parametrize("with_defaults", [False, True])
def test_missing_explicit_file_fails_before_start(option, with_defaults, tmp_path, capsys):
    if with_defaults:
        stored_credentials()
    missing = tmp_path / "missing"
    assert main(["sshmux", "-S", str(tmp_path / "socket"), option, str(missing)]) == 1
    assert option in capsys.readouterr().err
    assert not (tmp_path / "socket").exists()


def test_explicit_incomplete_credentials_fail(capsys):
    assert main(["sshmux", "-S", "/tmp/unused.sock", "--tls-cert", str(CERTS / "controller.crt")]) == 1
    assert "requires both --tls-cert and --tls-key" in capsys.readouterr().err


@pytest.fixture(params=["explicit", "stored"])
def server_options(request):
    options = ["--tls-hostname", "rmote.test"]
    if request.param == "stored":
        stored_credentials()
    else:
        options += [
            "--tls-ca",
            str(CERTS / "controller.crt"),
            "--tls-cert",
            str(CERTS / "controller.crt"),
            "--tls-key",
            str(CERTS / "controller.key"),
        ]
    return options


def test_tls_foreground_preserves_binary_session_and_exit_status(server: Server):
    payload = bytes(range(256)) * 1024
    result = server.run("host", "cat; printf stderr >&2; exit 7", input=payload)
    assert (result.returncode, result.stdout, result.stderr) == (7, payload, b"stderr")


@pytest.mark.parametrize("daemon_mode", [False, True])
@pytest.mark.parametrize("failure", ["hostname", "ca", "key"])
def test_invalid_tls_cannot_start_or_fall_back(daemon: Daemon, daemon_mode, failure):
    directory = stored_credentials()
    options = ["--tls-hostname", "rmote.test"]
    if failure == "hostname":
        options = ["--tls-hostname", "wrong.test"]
    elif failure == "ca":
        options += ["--tls-ca", str(CERTS / "untrusted.crt")]
    else:
        (directory / "controller.key").write_text("invalid key")
    command = daemon.command(*options)
    if not daemon_mode:
        command.remove("--daemon")
    result = subprocess.run(command, capture_output=True, timeout=15)
    assert result.returncode == 1, result.stderr
    assert not daemon.path.exists()
    assert result.stderr


def test_generated_defaults_start_tls_daemon_and_missing_explicit_file_still_fails(daemon: Daemon):
    if shutil.which("openssl") is None:
        pytest.skip("requires OpenSSL")
    result = subprocess.run([sys.executable, "-m", "rmote", "certs"], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    result = daemon.start()
    assert result.returncode == 0, result.stderr
    response = subprocess.run(daemon.client("host", "printf encrypted"), capture_output=True, timeout=10)
    assert (response.returncode, response.stdout) == (0, b"encrypted")
    # A failed explicit path is an error even when the daemon already exists.
    result = daemon.start("--tls-key", str(daemon.path.with_name("missing.key")))
    assert result.returncode == 1
    assert b"--tls-key" in result.stderr
