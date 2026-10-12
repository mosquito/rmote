# TLS over a transport

Pass a `TLSConfig` to protect the rmote byte stream with TLS 1.3. Serialization,
compression, headers, and frames all run above TLS. The configuration is opt-in;
connections without it keep their existing behavior.

Both processes need Python with the standard-library `ssl` module and TLS 1.3
support in OpenSSL. No third-party Python package is needed on either side.

## Controller certificate

The controller acts as the TLS server, even though it starts the transport.
It keeps the certificate and private key locally. The remote process acts as
the TLS client and verifies the controller's certificate chain and expected
name before exchanging rmote data. The certificate must allow TLS server
authentication and contain the expected name in its Subject Alternative Name.

All three paths below refer to files on the controller:

```python
from rmote.protocol import TLSConfig
from rmote.sync import Connection
from rmote.tools.exec import Exec

tls = TLSConfig(
    certfile="controller.crt",
    keyfile="controller.key",
    cafile="controller-ca.crt",
    server_hostname="controller.example.org",
    handshake_timeout=10.0,
)

with Connection.from_local(tls=tls) as remote:
    result = remote(Exec.command, "uname", "-s", capture_output=True)
    print(result.stdout)
```

The same `tls=` argument is available on synchronous `from_command` and
`from_ssh`, and on asynchronous `Protocol.from_command`, `from_ssh`, and
`from_subprocess`. Enter the asynchronous protocol context to complete TLS
and the rmote handshake:

```python
from rmote.protocol import Protocol

async def connect(tls: TLSConfig) -> None:
    async with await Protocol.from_command("ssh", "-T", "host", tls=tls) as remote:
        result = await remote(Exec.command, "uname", "-s", capture_output=True)
        print(result.stdout)
```

The TLS handshake has its own timeout. The synchronous client's
`connect_timeout` also bounds the complete connection setup. A failed TLS
handshake closes the connection; there is no fallback to plaintext.

## Trust boundary

The existing bootstrap sends executable Python, public CA certificates, and
the expected controller name through the original transport. TLS starts after
that bootstrap and before `PROTOCOL READY`. An attacker who can replace the
bootstrap can replace its verification code or trust configuration. Use a
trusted bootstrap transport, such as SSH with host verification; this option
does not make unauthenticated remote code delivery safe.

This configuration authenticates the controller to the remote process. It does
not authenticate the remote process to the controller and does not implement
mutual TLS. The underlying transport must establish the remote host's identity.
Only use rmote with trusted peers: TLS does not make received pickle data safe.

Application compression remains enabled. TLS hides frame contents, but does
not hide traffic lengths. Avoid compressing secrets together with data an
attacker controls when the attacker can observe those lengths.

## Local test credentials

The repository contains a certificate and a **publicly known private key** in
`tests/fixtures/tls`. For a local test, set `certfile` and `cafile` to
`tests/fixtures/tls/controller.crt`, `keyfile` to
`tests/fixtures/tls/controller.key`, and `server_hostname` to `rmote.test`.

These certificates last 100 years to keep the tests independent of routine
certificate renewal. **Never use them in production or install them in a
system trust store.** The adjacent README contains the generation command.
