# TLS over a transport

Pass a `TLSConfig` to protect the rmote byte stream with TLS 1.3. Serialization,
compression, headers, and frames all run above TLS. The configuration is opt-in;
connections without it keep their existing behavior.

Both processes need Python with the standard-library `ssl` module and TLS 1.3
support in OpenSSL. No third-party Python package is needed on either side.

## Why use TLS?

A transport can connect two processes without encrypting their traffic.
A plain TCP relay, such as `nc`, can carry rmote commands and results, but
anyone who can observe that connection can read the transferred data.
Serialization and compression do not protect secrets in arguments, file
contents, environment variables, or command output.

TLS adds encryption to the rmote stream independently of the transport.
After a trusted bootstrap and successful TLS handshake, it protects tool code,
RPC arguments, results, and protocol frames from reading or undetected changes
in transit. This is useful when the available transport only carries bytes.
SSH already protects its connection; rmote TLS is optional when using SSH.

The current implementation starts TLS **after the plaintext bootstrap**.
It can protect subsequent traffic from passive observation on an open channel,
but it does not secure an unauthenticated bootstrap against an active attacker.
An attacker who replaces the bootstrap can also replace its certificate checks.
The bootstrap must reach the intended peer without modification; see
{ref}`tls-trust-boundary` before using an unencrypted transport.

An unencrypted Telnet connection has the same confidentiality problem.
However, Telnet also carries control commands and negotiates terminal behavior,
as described in [RFC 854](https://www.rfc-editor.org/rfc/rfc854.html).
A Telnet transport would need an adapter that handles those commands and
preserves binary data in both directions. rmote does not provide that adapter.
TLS inside rmote would not protect Telnet login credentials or other data sent
before the TLS handshake.

## Controller certificate

The controller acts as the TLS server, even though it starts the transport.
It keeps the certificate and private key locally. The remote process acts as
the TLS client and verifies the controller's certificate chain and expected
name before exchanging rmote data. The certificate must allow TLS server
authentication and contain the expected name in its Subject Alternative Name.

### Generate a certificate with the CLI

Run this command on the controller:

```sh
rmote certs
```

It creates a self-signed `controller.crt` and an unencrypted `controller.key`,
valid for 365 days. The command prints a `TLSConfig` example with absolute paths.
Use the same certificate for `certfile` and `cafile`. The remote process trusts
that certificate directly; replacing it also changes the trust configuration.

The default directory is `$XDG_DATA_HOME/rmote/tls`, or
`~/.local/share/rmote/tls` when `XDG_DATA_HOME` is unset, empty, or relative.
This follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/).
The command creates the certificate directory with mode `0700` and the private
key with mode `0600`. It refuses an existing output directory.

Use `--output-dir` to select a new directory and `--days` to change the lifetime.
The default verified name is the controller's current hostname from
`socket.gethostname()`. Use `--hostname` to select another ASCII DNS name or
IP address. The name does not need to resolve for this stream transport.

```sh
rmote certs --hostname controller.example.org --days 90 \
  --output-dir ./controller-tls --openssl-bin /path/to/openssl
```

`--openssl-bin` defaults to the executable found by `shutil.which("openssl")`.
If none is found, argparse requires an explicit `--openssl-bin` argument.
The helper checks that the executable runs before it creates output directories.
Other rmote commands do not require the OpenSSL executable.

Keep the private key on the controller. Never commit it or send it to the remote
process. The helper uses an OpenSSL configuration file and also supports LibreSSL.
For a separate CA that can sign replacement certificates, use the commands below.

### Generate certificates with a separate CA

Run these commands on the controller with OpenSSL 1.1.1 or later. They create
a private CA valid for 10 years and a controller certificate valid for one year.
Replace `controller.example.org` in both the commands and `TLSConfig` with the
name you want to verify. For this stream transport, the name identifies the
controller; it does not need to resolve in DNS.

Check `openssl version` first. On macOS, the system command may be LibreSSL;
use an OpenSSL installation for the commands below, including hostname verification.

Create a new directory and restrict access to the generated keys:

```sh
umask 077
mkdir rmote-tls
cd rmote-tls
```

Create the CA certificate and its signing key:

```sh
openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
  -keyout controller-ca.key -out controller-ca.crt -days 3650 \
  -subj '/CN=rmote controller CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash'
```

Create a separate controller key and certificate signing request:

```sh
openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout controller.key -out controller.csr \
  -subj '/CN=controller.example.org'
```

Set the server identity and permitted uses, then sign the request:

```sh
cat > controller.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:controller.example.org
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

openssl x509 -req -in controller.csr \
  -CA controller-ca.crt -CAkey controller-ca.key -CAcreateserial \
  -out controller.crt -days 365 -sha256 -extfile controller.ext

openssl verify -x509_strict -CAfile controller-ca.crt \
  -purpose sslserver -verify_hostname controller.example.org controller.crt
```

The verification command must print `controller.crt: OK`. The connection uses
`controller.crt`, `controller.key`, and `controller-ca.crt`; it does not need
`controller-ca.key`. Keep the CA key separately for future certificate signing.
Both private keys are unencrypted, so restrict their file access and never
send them to the remote process or commit them to version control. Renew the
controller certificate before it expires, using the same CA if you want to
keep the existing trust configuration.

See the OpenSSL documentation for
[`req`](https://docs.openssl.org/3.0/man1/openssl-req/) and
[`x509`](https://docs.openssl.org/3.0/man1/openssl-x509/) for command options.

### Connect with the certificates

Run the example from `rmote-tls`, or use absolute paths in `TLSConfig`.
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

(tls-trust-boundary)=
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
