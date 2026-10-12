"""Certificate generation, CLI failures, and use by the TLS transport."""

import os
import shutil
import ssl
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rmote.cli import build_parser, certs, main
from rmote.protocol import TLSConfig
from rmote.sync import Connection
from tests.sync.tools import Methods


def test_openssl_argument_default_and_override(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/tools/openssl")
    parser = build_parser()
    assert parser.parse_args(["certs"]).openssl_bin == "/tools/openssl"
    assert parser.parse_args(["certs", "--openssl-bin", "/other/openssl"]).openssl_bin == "/other/openssl"


def test_missing_openssl_requires_argument(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["certs"])
    assert error.value.code == 2
    assert "--openssl-bin" in capsys.readouterr().err
    assert parser.parse_args(["certs", "--openssl-bin", "/custom/openssl"]).openssl_bin == "/custom/openssl"
    assert parser.parse_args(["shell"]).transport == []


@pytest.mark.parametrize("xdg", [None, "", "relative/path", "/custom/data"])
def test_xdg_directory(monkeypatch, tmp_path, xdg):
    monkeypatch.setenv("HOME", str(tmp_path))
    if xdg is None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_DATA_HOME", xdg)
    base = Path("/custom/data") if xdg == "/custom/data" else tmp_path / ".local/share"
    assert build_parser().parse_args(["certs", "--openssl-bin", "openssl"]).output_dir == base / "rmote/tls"


@pytest.mark.parametrize(
    "option,value",
    [
        ("--days", "0"),
        ("--days", "-1"),
        ("--days", "many"),
        ("--hostname", ""),
        ("--hostname", "foo\n[section]"),
        ("--hostname", "foo,DNS:other"),
        ("--hostname", "*.example.org"),
        ("--hostname", "fe80::1%zone\n[section]"),
    ],
)
def test_invalid_certificate_arguments(option, value):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["certs", "--openssl-bin", "openssl", option, value])
    assert error.value.code == 2


def test_invalid_executable_does_not_create_output(tmp_path, capsys):
    output = tmp_path / "new-parent" / "tls"
    assert main(["certs", "--openssl-bin", str(tmp_path / "missing"), "--output-dir", str(output)]) == 1
    assert not output.parent.exists()
    assert "No such file" in capsys.readouterr().err


def test_failure_cleans_up_partial_credentials(tmp_path, monkeypatch, capsys):
    output = tmp_path / "tls"

    def fail_generation(binary, *arguments):
        if arguments[0] == "req":
            (output / "controller.key").write_text("partial key")
            raise RuntimeError("generation failed")

    monkeypatch.setattr(certs, "_openssl", fail_generation)
    assert main(["certs", "--openssl-bin", "openssl", "--output-dir", str(output)]) == 1
    assert not output.exists()
    assert "generation failed" in capsys.readouterr().err


def test_existing_directory_is_unchanged(tmp_path, monkeypatch, capsys):
    output = tmp_path / "tls"
    output.mkdir()
    key = output / "controller.key"
    key.write_text("existing key")
    monkeypatch.setattr(certs, "_openssl", lambda *args: None)
    assert main(["certs", "--openssl-bin", "openssl", "--output-dir", str(output)]) == 1
    assert key.read_text() == "existing key"
    assert "File exists" in capsys.readouterr().err


def test_openssl_process_error_is_reported():
    with pytest.raises(RuntimeError, match="failed .*openssl failed"):
        certs._openssl(sys.executable, "-c", "import sys; sys.stderr.write('openssl failed'); sys.exit(3)")


@pytest.mark.parametrize("interrupted", [False, True], ids=["timeout", "interrupt"])
def test_openssl_process_is_reaped_on_interruption(monkeypatch, interrupted):
    communicate = subprocess.Popen.communicate
    processes = []

    def short_communicate(process, input=None, timeout=None):
        if timeout is not None:
            processes.append(process)
            if interrupted:
                raise KeyboardInterrupt
            timeout = 0.05
        return communicate(process, input=input, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", short_communicate)
    exception = KeyboardInterrupt if interrupted else subprocess.TimeoutExpired
    with pytest.raises(exception):
        certs._openssl(sys.executable, "-c", "import time; time.sleep(60)")
    assert len(processes) == 1
    assert processes[0].poll() is not None


@pytest.mark.parametrize("name", ["controller.example.org", "127.0.0.1", "::1"])
def test_generated_certificate_supports_tls(tmp_path, name):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is not installed")
    result = subprocess.run(
        [sys.executable, "-m", "rmote", "certs", "--hostname", name, "--days", "2"],
        env={**os.environ, "XDG_DATA_HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    directory = tmp_path / "rmote" / "tls"
    certificate, key = directory / "controller.crt", directory / "controller.key"
    assert set(directory.iterdir()) == {certificate, key}
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert f"server_hostname={name!r}" in result.stdout
    assert str(certificate) in result.stdout
    dates = subprocess.check_output(
        [openssl, "x509", "-in", str(certificate), "-noout", "-dates"], text=True, timeout=5
    ).splitlines()
    start, end = (ssl.cert_time_to_seconds(date.split("=", 1)[1]) for date in dates)
    assert end - start == 2 * 86400
    config = TLSConfig(str(certificate), str(key), str(certificate), name)
    with Connection.from_local(tls=config) as remote:
        assert remote(Methods.echo, "generated certificate") == "generated certificate"
    # The generated SAN must be checked, even though this is a directly trusted certificate.
    wrong_name = TLSConfig(str(certificate), str(key), str(certificate), "wrong.example.org")
    with pytest.raises(ssl.SSLError):
        with Connection.from_local(tls=wrong_name):
            pytest.fail("TLS accepted an incorrect hostname")
