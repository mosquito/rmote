"""All connection commands share TLS defaults, overrides, and opt-out rules."""

import shutil

import pytest

from rmote.cli import build_parser, main
from rmote.cli.certs import default_directory
from rmote.cli.tls import tls_config
from tests.support.tls import CERTS


@pytest.fixture(params=["shell", "repl", "rsync", "sshmux"])
def command(request, tmp_path):
    args = [request.param]
    if request.param == "sshmux":
        args += ["-S", str(tmp_path / "socket")]
    elif request.param == "rsync":
        args += [str(tmp_path / "source"), "remote:/unused"]
    return args


@pytest.fixture
def stored():
    directory = default_directory()
    directory.mkdir(parents=True)
    for name in ("controller.crt", "controller.key"):
        shutil.copyfile(CERTS / name, directory / name)
    return directory


def test_common_defaults_and_no_tls(command, stored):
    parser = build_parser()
    args = parser.parse_args(command)
    assert args.tls_cert == args.tls_ca == stored / "controller.crt"
    assert args.tls_key == stored / "controller.key"
    assert tls_config(args) is not None
    assert tls_config(parser.parse_args([*command, "--no-tls"])) is None


def test_no_tls_ignores_invalid_default_credentials(command, stored):
    (stored / "controller.key").write_text("invalid key")
    assert tls_config(build_parser().parse_args([*command, "--no-tls"])) is None


@pytest.mark.parametrize("option", ["--tls-ca", "--tls-cert", "--tls-key"])
def test_missing_explicit_file_is_reported_without_traceback(command, option, tmp_path, capsys):
    assert main([*command, option, str(tmp_path / "missing")]) == 1
    error = capsys.readouterr().err
    assert option in error
    assert "Traceback" not in error


def test_no_tls_rejects_explicit_tls_files(command, stored, capsys):
    assert main([*command, "--no-tls", "--tls-cert", str(stored / "controller.crt")]) == 1
    assert "--no-tls cannot be combined" in capsys.readouterr().err
