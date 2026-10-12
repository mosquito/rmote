"""Create a self-signed controller certificate with the OpenSSL executable."""

import argparse
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def default_directory() -> Path:
    """Use the XDG data directory, ignoring empty or relative values."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data_home.is_absolute():
        data_home = Path.home() / ".local" / "share"
    return data_home / "rmote" / "tls"


def hostname(value: str) -> str:
    """Accept a DNS name or IP address without OpenSSL configuration syntax."""
    if "%" in value:
        raise argparse.ArgumentTypeError("scoped IP addresses are not supported")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if len(value) > 253 or not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in value.split(".")
    ):
        raise argparse.ArgumentTypeError("expected an ASCII DNS name or IP address")
    return value


def positive_days(value: str) -> int:
    """Parse a positive certificate lifetime in days."""
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive number of days") from None
    if days < 1:
        raise argparse.ArgumentTypeError("expected a positive number of days")
    return days


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Configure certificate generation arguments."""
    openssl_bin = shutil.which("openssl")
    parser.set_defaults(__func__=run, __prog__=parser.prog)
    parser.add_argument(
        "--openssl-bin",
        default=openssl_bin,
        required=openssl_bin is None,
        metavar="PATH",
        help="OpenSSL executable; required when openssl is absent from PATH.",
    )
    parser.add_argument(
        "--hostname", type=hostname, default=socket.gethostname(), help="Controller DNS name or IP address."
    )
    parser.add_argument("--days", type=positive_days, default=365, help="Certificate lifetime in days.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_directory(),
        help="New directory for the certificate and private key.",
    )


def _openssl(binary: str, *arguments: str) -> None:
    """Run OpenSSL without a shell, with private files and a bounded wait."""
    with subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        umask=0o077,
    ) as process:
        try:
            _, stderr = process.communicate(timeout=30)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(f"OpenSSL {arguments[0]} failed ({process.returncode}): {stderr.strip()}")


def _generate(binary: str, directory: Path, name: str, days: int) -> None:
    # Check the executable before creating any output directories.
    _openssl(binary, "version")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        try:
            ipaddress.ip_address(name)
            alternative_name = f"IP:{name}"
        except ValueError:
            alternative_name = f"DNS:{name}"
        with tempfile.TemporaryDirectory(prefix=".openssl-", dir=directory) as temporary:
            config = Path(temporary) / "openssl.cnf"
            config.write_text(
                "[req]\nprompt = no\ndistinguished_name = subject\nx509_extensions = server\n"
                "[subject]\nCN = rmote controller\n"
                "[server]\nbasicConstraints = critical,CA:FALSE\n"
                "keyUsage = critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage = serverAuth\n"
                f"subjectAltName = {alternative_name}\n"
                "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n",
                encoding="ascii",
            )
            _openssl(
                binary,
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-sha256",
                "-nodes",
                "-batch",
                "-config",
                str(config),
                "-days",
                str(days),
                "-keyout",
                str(directory / "controller.key"),
                "-out",
                str(directory / "controller.crt"),
            )
    except BaseException:
        shutil.rmtree(directory)
        raise


def run(args: argparse.Namespace) -> int:
    """Generate credentials and print the corresponding TLS configuration."""
    directory = args.output_dir.expanduser().absolute()
    try:
        _generate(args.openssl_bin, directory, args.hostname, args.days)
    except KeyboardInterrupt:
        print(f"{args.__prog__}: interrupted", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"{args.__prog__}: {error}", file=sys.stderr)
        return 1
    certificate = str(directory / "controller.crt")
    key = str(directory / "controller.key")
    print(
        "from rmote.protocol import TLSConfig\n\n"
        "tls = TLSConfig(\n"
        f"    certfile={certificate!r},\n"
        f"    keyfile={key!r},\n"
        f"    cafile={certificate!r},\n"
        f"    server_hostname={args.hostname!r},\n"
        ")"
    )
    return 0
