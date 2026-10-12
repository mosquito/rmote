"""One transport security matrix for API, tool, documentation, and CLI tests."""

from pathlib import Path

import pytest

from rmote.protocol import TLSConfig

CERTS = Path(__file__).parents[1] / "fixtures" / "tls"


@pytest.fixture(params=[False, True], ids=["plaintext", "tls"])
def transport_tls(request: pytest.FixtureRequest) -> TLSConfig | None:
    return tls_for_mode(request.param)


def tls_for_mode(enabled: bool) -> TLSConfig | None:
    if not enabled:
        return None
    return TLSConfig(
        certfile=str(CERTS / "controller.crt"),
        keyfile=str(CERTS / "controller.key"),
        cafile=str(CERTS / "controller.crt"),
        server_hostname="rmote.test",
    )


@pytest.fixture
def tls_cli_args(transport_tls: TLSConfig | None) -> list[str]:
    if transport_tls is None:
        return ["--no-tls"]
    return [
        "--tls-cert",
        transport_tls.certfile,
        "--tls-key",
        transport_tls.keyfile,
        "--tls-ca",
        transport_tls.cafile,
        "--tls-hostname",
        transport_tls.server_hostname,
    ]
