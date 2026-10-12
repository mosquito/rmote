"""Shared TLS arguments, local credential defaults, and validation."""

import argparse
import socket
from pathlib import Path

from rmote.cli.certs import default_directory
from rmote.protocol import TLSConfig


def default_tls_file(name: str) -> Path | None:
    """Find an existing stored TLS file."""
    path = default_directory() / name
    return path if path.is_file() else None


class TLSFileAction(argparse.Action):
    """Track explicit TLS paths separately from discovered argparse defaults."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        namespace._tls_explicit = (*getattr(namespace, "_tls_explicit", ()), self.dest)


def tls_config(args: argparse.Namespace) -> TLSConfig | None:
    """Use complete stored credentials, or require valid explicitly selected files."""
    explicit = getattr(args, "_tls_explicit", ())
    if args.no_tls:
        if explicit:
            raise ValueError("--no-tls cannot be combined with explicit TLS files")
        return None
    for option in explicit:
        path = getattr(args, option)
        if not path.expanduser().is_file():
            raise ValueError(f"--{option.replace('_', '-')} is not a file: {path}")
    cert, key = args.tls_cert, args.tls_key
    if cert is None or key is None or not cert.expanduser().is_file() or not key.expanduser().is_file():
        if not explicit:
            return None
        raise ValueError("TLS requires both --tls-cert and --tls-key, or their files in the rmote XDG directory")
    ca = args.tls_ca if "tls_ca" in explicit else default_tls_file("controller-ca.crt") or cert
    return TLSConfig(
        certfile=str(cert.expanduser()),
        keyfile=str(key.expanduser()),
        cafile=str(ca.expanduser()),
        server_hostname=args.tls_hostname,
    )


def configure_tls_parser(parser: argparse.ArgumentParser) -> None:
    """Add TLS options to a command that opens an rmote connection."""
    parser.add_argument(
        "--tls-ca",
        type=Path,
        action=TLSFileAction,
        default=default_tls_file("controller-ca.crt") or default_tls_file("controller.crt"),
        metavar="PATH",
        help="Controller CA certificate; use the stored controller-ca.crt, or the selected TLS certificate.",
    )
    parser.add_argument(
        "--tls-cert",
        type=Path,
        action=TLSFileAction,
        default=default_tls_file("controller.crt"),
        metavar="PATH",
        help="Controller TLS certificate; use controller.crt in the rmote XDG data directory when present.",
    )
    parser.add_argument(
        "--tls-key",
        type=Path,
        action=TLSFileAction,
        default=default_tls_file("controller.key"),
        metavar="PATH",
        help="Controller TLS private key; use controller.key in the rmote XDG data directory when present.",
    )
    parser.add_argument(
        "--tls-hostname",
        default=socket.gethostname(),
        metavar="NAME",
        help="Controller name verified by the remote TLS client; must match the certificate.",
    )
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS, including stored default certificates.")
